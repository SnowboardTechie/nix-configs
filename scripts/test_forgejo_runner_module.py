#!/usr/bin/env python3
"""services.forgejo-runner: Studio as configured, and its launcher against stand-ins.

Evaluates through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk (a git flake hides untracked files). Nothing is
built, installed or started: the launcher runs against a stand-in runner, a
temporary state directory and a fake token, never the real binary or files.
"""
import json
import secrets
import stat
import subprocess
import tempfile
import time
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "modules" / "services" / "forgejo-runner-launcher.sh"
STATE = "/var/lib/forgejo-runner"
LOGS = "/var/log/forgejo-runner"
USER = "_forgejo-runner"
PLACEHOLDER = "@FORGEJO_RUNNER_UUID@"
UUID = "0f9e8d7c-6b5a-4938-a271-605f4e3d2c1b"


def nix(*args):
    result = subprocess.run(["nix", *args], cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(settings, expr):
    """`expr` with `config` and `pkgs` bound to Studio plus `services.forgejo-runner = settings`."""
    return f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
        pkgs = studio.pkgs;
        inherit (pkgs) lib;
        config = (studio.extendModules {{
          modules = [ {{ services.forgejo-runner = {settings}; }} ];
        }}).config;
      in {expr}
    """


@cache
def studio(settings, expr):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(settings, expr)))


FAILED = "map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)"
DAEMON = "config.launchd.daemons.forgejo-runner.serviceConfig"
SUMMARY = f"""{{
  enable = config.services.forgejo-runner.enable;
  daemons = builtins.attrNames config.launchd.daemons;
  agents = builtins.attrNames config.launchd.user.agents ++ builtins.attrNames config.launchd.agents;
  knownUsers = config.users.knownUsers;
  knownGroups = config.users.knownGroups;
  users = builtins.attrNames config.users.users;
  failed = {FAILED};
}}"""


def plist():
    return studio("{ }", DAEMON)


def config_text():
    return studio("{ }", "config.services.forgejo-runner.configText")


class StudioAsConfigured(unittest.TestCase):
    """Studio runs one system daemon as the hidden runner account, with no secret anywhere in Nix."""

    def test_enabled_without_failed_assertions(self):
        s = studio("{ }", SUMMARY)
        self.assertTrue(s["enable"])
        self.assertEqual([], s["failed"])
        self.assertIn("forgejo-runner", s["daemons"])
        self.assertFalse([a for a in s["agents"] if "forgejo" in a])
        self.assertEqual([USER], s["knownUsers"])
        self.assertEqual([USER], s["knownGroups"])

    def test_package_is_nixpkgs_forgejo_runner_13_2_1(self):
        self.assertEqual("13.2.1", studio("{ }", "config.services.forgejo-runner.package.version"))
        self.assertEqual(
            studio("{ }", '"${pkgs.forgejo-runner}"'),
            studio("{ }", '"${config.services.forgejo-runner.package}"'),
        )

    def test_account_is_hidden_unprivileged_and_homed_in_its_state(self):
        u = studio("{ }", f'config.users.users."{USER}"')
        g = studio("{ }", f'config.users.groups."{USER}"')
        self.assertEqual(
            {"uid": 450, "gid": 450, "home": STATE, "shell": "/usr/bin/false", "isHidden": True, "createHome": False},
            {k: u[k] for k in ("uid", "gid", "home", "shell", "isHidden", "createHome")},
        )
        self.assertEqual({"gid": 450, "members": []}, {k: g[k] for k in ("gid", "members")})
        # No managed group lists it (nix-darwin manages only these known groups).
        members = studio("{ }", "lib.mapAttrs (_: g: g.members) config.users.groups")
        self.assertEqual({USER: []}, {k: v for k, v in members.items() if v or k == USER})

    def test_daemon_runs_as_the_account_through_bin_sh(self):
        p = plist()
        self.assertEqual("org.nixos.forgejo-runner", p["Label"])
        self.assertEqual(USER, p["UserName"])
        self.assertEqual(USER, p["GroupName"])
        self.assertTrue(p["RunAtLoad"])
        self.assertTrue(p["KeepAlive"])
        self.assertEqual(STATE, p["WorkingDirectory"])
        self.assertEqual(f"{LOGS}/runner.log", p["StandardOutPath"])
        self.assertEqual(f"{LOGS}/runner.error.log", p["StandardErrorPath"])
        # launchd must outwait the runner's 30-minute shutdown_timeout.
        self.assertGreater(p["ExitTimeOut"], 30 * 60)
        args = p["ProgramArguments"]
        self.assertEqual(["/bin/sh", "-c"], args[:2])
        self.assertEqual("wait-for-nix-store", args[3])
        launcher, runner, state, template, poll = args[4:]
        self.assertTrue(launcher.startswith("/nix/store/") and launcher.endswith("-forgejo-runner-launcher.sh"))
        self.assertEqual(LAUNCHER.read_text(), studio("{ }", f"builtins.readFile (builtins.elemAt {DAEMON}.ProgramArguments 4)"))
        self.assertEqual(studio("{ }", '"${pkgs.forgejo-runner}/bin/forgejo-runner"'), runner)
        self.assertEqual(STATE, state)
        self.assertTrue(template.endswith("-forgejo-runner-config-template.yaml"))
        self.assertEqual("30", poll)

    def test_daemon_environment_is_home_and_path_only(self):
        env = plist()["EnvironmentVariables"]
        self.assertEqual({"HOME", "PATH"}, set(env))
        self.assertEqual(STATE, env["HOME"])
        path = env["PATH"].split(":")
        node, git = path[:2]
        self.assertRegex(node, r"^/nix/store/[a-z0-9]{32}-nodejs-26\.13\.0/bin$")
        self.assertRegex(git, r"^/nix/store/[a-z0-9]{32}-git-[0-9.]+/bin$")
        self.assertEqual(["/usr/bin", "/bin", "/usr/sbin", "/sbin"], path[2:])
        self.assertNotIn("/opt/homebrew", env["PATH"])

    def test_activation_creates_state_and_logs_after_the_account(self):
        users = studio("{ }", "config.system.activationScripts.users.text")
        extra = studio("{ }", "config.system.activationScripts.extraActivation.text")
        block = users[users.index("# === forgejo-runner state and logs ==="):]
        self.assertLess(users.index("setting up users"), users.index("# === forgejo-runner state and logs ==="))
        for line in (
            f"/usr/sbin/chown {USER}:{USER} {STATE}",
            f"/bin/chmod 0700 {STATE}",
            f"/usr/sbin/chown root:wheel {LOGS}",
            f"/bin/chmod 0755 {LOGS}",
            f"/usr/sbin/chown {USER}:admin {LOGS}/runner.log {LOGS}/runner.error.log",
            f"/bin/chmod 0640 {LOGS}/runner.log {LOGS}/runner.error.log",
        ):
            self.assertIn(line, block)
        self.assertNotIn("forgejo", extra)
        # Nothing at activation touches the registration files.
        self.assertNotIn(f"{STATE}/token", block)
        self.assertNotIn(f"{STATE}/uuid", block)

    def test_config_template(self):
        c = json.loads(config_text())
        envs = c["runner"].pop("envs")
        self.assertEqual(
            {
                "log": {"level": "info", "job_level": "info"},
                "runner": {
                    "file": f"{STATE}/.runner",
                    "capacity": 1,
                    "timeout": "90m",
                    "shutdown_timeout": "30m",
                },
                "cache": {"enabled": False},
                "host": {"workdir_parent": f"{STATE}/work"},
                "server": {
                    "connections": {
                        "forgejo": {
                            "url": "https://git.snowboardtechie.com/",
                            "uuid": PLACEHOLDER,
                            "token_url": f"file:{STATE}/token",
                            "labels": ["studio-gate:host"],
                        }
                    }
                },
            },
            c,
        )
        self.assertEqual(
            {"PLANNING_MATRIX_PYTHON", "SGG_TEST_HERMES_PYTHON", "PLANNING_WEB_BROWSER_APPROVAL", "npm_config_cache"},
            set(envs),
        )
        matrix_python = studio("{ }", '"${config.services.forgejo-runner.matrixPython}"')
        self.assertEqual(f"{matrix_python}/bin/python3", envs["PLANNING_MATRIX_PYTHON"])
        self.assertEqual("/Users/Shared/durable-pi-ci/browser/browser-approval.json", envs["PLANNING_WEB_BROWSER_APPROVAL"])
        self.assertEqual(f"{STATE}/npm-cache", envs["npm_config_cache"])
        # The template file is exactly this text.
        self.assertEqual(
            studio("{ }", f"(builtins.elemAt {DAEMON}.ProgramArguments 7)"),
            studio("{ }", '"${pkgs.writeText "forgejo-runner-config-template.yaml" config.services.forgejo-runner.configText}"'),
        )

    def test_hermes_python_is_the_installed_hermes_env(self):
        envs = json.loads(config_text())["runner"]["envs"]
        venv = studio("{ }", '"${config.services.hermes.package.passthru.hermesVenv}"')
        self.assertRegex(venv, r"^/nix/store/[a-z0-9]{32}-hermes-agent-env$")
        self.assertEqual(f"{venv}/bin/python3", envs["SGG_TEST_HERMES_PYTHON"])
        # That Hermes is the one on the system path.
        self.assertIn(
            studio("{ }", '"${config.services.hermes.package}"'),
            studio("{ }", 'map (p: "${p}") config.environment.systemPackages'),
        )

    def test_no_secret_reaches_nix(self):
        text = config_text()
        everything = json.dumps(plist()) + text
        self.assertNotIn("/Users/bryan", everything)
        self.assertNotIn(".secrets", everything)
        # The token appears only as token_url's file, never as a value.
        self.assertNotIn('"token"', text)
        self.assertEqual(1, text.count(f'"token_url":"file:{STATE}/token"'))
        self.assertNotIn("token", text.replace(f'"token_url":"file:{STATE}/token"', ""))
        self.assertNotIn("token", json.dumps(plist()))


class MatrixPython(unittest.TestCase):
    """mautrix 0.21.1 with olm, the insecure libolm permitted for this one Python only."""

    def test_packages(self):
        names = studio("{ }", 'map (p: "${p.pname or ""}=${p.version or ""}") config.services.forgejo-runner.matrixPython.paths')
        for package in ("mautrix=0.21.1", "python-olm=3.2.16", "pycryptodome", "unpaddedbase64", "base58", "aiohttp"):
            self.assertTrue(any(n.startswith(package) for n in names), (package, names))

    def test_olm_permitted_only_here(self):
        olm = studio(
            "{ }",
            """let po = builtins.head (builtins.filter (p: (p.pname or "") == "python-olm")
                       config.services.forgejo-runner.matrixPython.paths);
               in map (o: o.meta.knownVulnerabilities) (builtins.filter (p: (p.pname or "") == "olm") po.buildInputs)""",
        )
        self.assertEqual([[]], olm)
        # nixpkgs' own olm is still refused, and nothing permits it host-wide.
        self.assertFalse(studio("{ }", "(builtins.tryEval pkgs.olm.drvPath).success"))
        self.assertFalse(studio("{ }", "config.nixpkgs.config ? permittedInsecurePackages"))
        self.assertEqual([], studio("{ }", "pkgs.config.permittedInsecurePackages or [ ]"))
        # The system still evaluates with it (nothing built).
        self.assertTrue(studio("{ }", 'lib.hasSuffix ".drv" config.system.build.toplevel.drvPath'))


class Refusals(unittest.TestCase):
    def test_labels_required(self):
        failed = studio("{ labels = lib.mkForce [ ]; }", FAILED)
        self.assertIn("services.forgejo-runner.labels must name at least one label", failed)

    def test_https_required(self):
        failed = studio('{ url = lib.mkForce "http://git.snowboardtechie.com/"; }', FAILED)
        self.assertIn("services.forgejo-runner.url must be https://", failed)

    def test_disabled_declares_nothing(self):
        s = studio("{ enable = lib.mkForce false; }", SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertNotIn("forgejo-runner", s["daemons"])
        self.assertNotIn(USER, s["knownUsers"])
        self.assertNotIn(USER, s["users"])
        self.assertNotIn("forgejo-runner", studio("{ enable = lib.mkForce false; }", "config.system.activationScripts.users.text"))


STAND_IN = """#!/bin/sh
# Stand-in for forgejo-runner: records its arguments and the config it was given.
printf '%s\\n' "$@" > "$RECORD/args"
cp "$3" "$RECORD/config"
pwd > "$RECORD/cwd"
exit 0
"""


class Launcher(unittest.TestCase):
    """The launcher against a stand-in runner, a temporary state directory and a fake token."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="forgejo-runner-test."))
        self.state = self.tmp / "state"
        self.state.mkdir(mode=0o700)
        self.record = self.tmp / "record"
        self.record.mkdir()
        self.runner = self.tmp / "forgejo-runner"
        self.runner.write_text(STAND_IN)
        self.runner.chmod(0o755)
        self.template = self.tmp / "template.yaml"
        self.template.write_text(config_text())
        self.token = "fake-" + secrets.token_hex(20)
        self.stderr = self.tmp / "stderr"
        self.started = None
        self.stderr_file = None

    @property
    def proc(self):
        assert self.started is not None, "the launcher was not started"
        return self.started

    def tearDown(self):
        if self.started and self.started.poll() is None:
            self.started.kill()
            self.started.wait()
        if self.stderr_file:
            self.stderr_file.close()
        subprocess.run(["rm", "-rf", str(self.tmp)], check=True)

    def start(self, argv=None):
        argv = argv or ["/bin/sh", str(LAUNCHER), str(self.runner), str(self.state), str(self.template), "0.1"]
        self.stderr_file = self.stderr.open("w")
        self.started = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=self.stderr_file,
            env={"PATH": "/usr/bin:/bin", "RECORD": str(self.record)},
            cwd=self.tmp,
        )

    def write_uuid(self, text):
        (self.state / "uuid").write_text(text)

    def write_token(self, mode=0o600):
        path = self.state / "token"
        path.write_text(self.token + "\n")
        path.chmod(mode)

    def assert_still_waiting(self, seconds=0.8):
        time.sleep(seconds)
        self.assertIsNone(self.proc.poll(), self.stderr.read_text())
        self.assertFalse((self.record / "args").exists())
        self.assertFalse((self.state / "config.yaml").exists())

    def assert_started(self):
        self.assertEqual(0, self.proc.wait(timeout=10), self.stderr.read_text())
        self.assertEqual(["daemon", "--config", f"{self.state}/config.yaml"], (self.record / "args").read_text().splitlines())
        self.assertEqual(str(self.state.resolve()), str(Path((self.record / "cwd").read_text().strip()).resolve()))
        config = self.state / "config.yaml"
        self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
        self.assertEqual(self.template.read_text().replace(PLACEHOLDER, UUID), config.read_text())
        self.assertNotIn(self.token, config.read_text())
        self.assertNotIn(self.token, self.stderr.read_text())
        self.assertEqual([], [p.name for p in self.state.iterdir() if p.name.startswith(".config.yaml.")])

    def test_waits_without_exiting_until_both_files_exist(self):
        self.start()
        self.assert_still_waiting()
        self.write_uuid(UUID + "\n")
        self.assert_still_waiting()
        self.write_token()
        self.assert_started()
        err = self.stderr.read_text()
        # One waiting line across many polls, then the start; never the token.
        self.assertEqual(1, err.count("waiting for"), err)
        self.assertIn("registration found; starting the runner", err)

    def test_refuses_a_malformed_uuid_until_it_is_corrected(self):
        self.write_token()
        for bad in (
            UUID.upper(),
            UUID + "x",
            UUID + "\nlabels: [evil]",
            UUID.replace("-", ""),
            " " + UUID,
            UUID[:-1] + '"',
            "not-a-uuid",
        ):
            with self.subTest(bad=bad):
                self.write_uuid(bad)
                if self.started is None:
                    self.start()
                self.assert_still_waiting(0.4)
        self.assertIn("refusing", self.stderr.read_text())
        self.write_uuid(UUID)
        self.assert_started()

    def test_refuses_a_token_others_can_read(self):
        self.write_uuid(UUID)
        self.write_token(mode=0o644)
        self.start()
        self.assert_still_waiting()
        self.assertIn("readable by others", self.stderr.read_text())
        (self.state / "token").chmod(0o600)
        self.assert_started()

    def test_refuses_a_template_without_the_placeholder(self):
        self.template.write_text('{"server":{}}')
        self.write_uuid(UUID)
        self.write_token()
        self.start()
        self.assertEqual(78, self.proc.wait(timeout=10))
        self.assertFalse((self.state / "config.yaml").exists())
        self.assertFalse((self.record / "args").exists())

    def test_daemon_wrapper_waits_for_the_store(self):
        """The plist's own /bin/sh wrapper, with the runner not yet present (Nix volume unmounted)."""
        wrapper = plist()["ProgramArguments"][:4]
        self.runner.rename(self.tmp / "absent")
        self.write_uuid(UUID)
        self.write_token()
        self.start(wrapper + [str(LAUNCHER), str(self.runner), str(self.state), str(self.template), "0.1"])
        time.sleep(1.5)
        self.assertIsNone(self.proc.poll(), self.stderr.read_text())
        self.assertFalse((self.record / "args").exists())
        (self.tmp / "absent").rename(self.runner)
        self.assert_started()


if __name__ == "__main__":
    unittest.main()
