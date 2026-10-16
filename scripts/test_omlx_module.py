#!/usr/bin/env python3
"""services.omlx: disabled on Studio; enabled, one loopback launchd agent and nothing else.

Evaluates through a `path:` flake reference so an uncommitted module is
evaluated as it is on disk (a git flake hides untracked files). Nothing here
installs, downloads, starts or activates anything: the preflight runs against
a stand-in executable in a temporary directory.
"""
import json
import os
import plistlib
import re
import stat
import subprocess
import tempfile
import unittest
from functools import cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PREFLIGHT = REPO / "modules" / "services" / "omlx-preflight.sh"
HOME = "/Users/bryan"
STATE = f"{HOME}/.local/share/omlx-trial"
EX_CONFIG = 78


def nix(*args):
    result = subprocess.run(["nix", *args], cwd=REPO, text=True, capture_output=True)
    if result.returncode != 0:
        raise AssertionError(result.stderr[-4000:])
    return result.stdout


def studio_expr(settings, expr):
    """`expr` with `config` and `lib` bound to Studio plus `services.omlx = settings`."""
    return f"""
      let
        flake = builtins.getFlake "path:{REPO}";
        studio = flake.darwinConfigurations.studio;
        inherit (studio.pkgs) lib;
        config = (studio.extendModules {{
          modules = [ {{ services.omlx = {settings}; }} ];
        }}).config;
      in {expr}
    """


@cache
def studio(settings, expr):
    return json.loads(nix("eval", "--json", "--impure", "--expr", studio_expr(settings, expr)))


def refusal(settings):
    """The evaluation error for a setting the option type itself refuses."""
    result = subprocess.run(
        ["nix", "eval", "--json", "--impure", "--expr",
         studio_expr(settings, "config.launchd.user.agents.omlx.serviceConfig.ProgramArguments")],
        cwd=REPO, text=True, capture_output=True,
    )
    if result.returncode == 0:
        raise AssertionError(f"accepted {settings}: {result.stdout}")
    return result.stderr


FAILED = "map (a: a.message) (builtins.filter (a: !a.assertion) config.assertions)"
ACTIVATION = (
    "config.system.activationScripts.preActivation.text"
    " + config.system.activationScripts.extraActivation.text"
    " + config.system.activationScripts.postActivation.text"
)
SUMMARY = f"""{{
  omlx = {{ inherit (config.services.omlx) enable port stateDir memoryGuard ssdCacheGB; }};
  agents = builtins.attrNames config.launchd.user.agents;
  system = builtins.attrNames config.launchd.agents ++ builtins.attrNames config.launchd.daemons;
  taps = map (t: t.name or t) config.homebrew.taps;
  brews = map (b: b.name or b) config.homebrew.brews;
  casks = map (c: c.name or c) config.homebrew.casks;
  packages = map (p: p.name or "") config.environment.systemPackages;
  activation = {ACTIVATION};
  ollama = config.launchd.user.agents.ollama.serviceConfig;
  openWebUI = config.launchd.user.agents.open-webui.serviceConfig;
  failed = {FAILED};
}}"""
ENABLED = "{ enable = lib.mkForce true; }"
AGENT = "config.launchd.user.agents.omlx.serviceConfig"


class StudioAsConfigured(unittest.TestCase):
    """Studio imports the capability with explicit trial settings and leaves it off."""

    def test_disabled_with_explicit_trial_settings(self):
        s = studio("{ }", SUMMARY)
        self.assertEqual([], s["failed"])
        self.assertEqual(
            {"enable": False, "port": 8000, "stateDir": STATE, "memoryGuard": "safe", "ssdCacheGB": 20},
            s["omlx"],
        )
        self.assertEqual([], [name for name in s["agents"] + s["system"] if "omlx" in name])
        for item in s["taps"] + s["brews"] + s["casks"] + s["packages"]:
            self.assertNotIn("omlx", item.lower())
        self.assertNotIn("omlx", s["activation"].lower())

    def test_ollama_and_open_webui_are_unchanged(self):
        s = studio("{ }", SUMMARY)
        ollama = studio("{ }", "{ inherit (config.services.ollama) enable host port contextLength numParallel keepAlive maxLoadedModels flashAttention kvCacheType tailscaleServe; }")
        self.assertEqual(
            {
                "enable": True, "host": "127.0.0.1", "port": 11434, "contextLength": 65536,
                "numParallel": 1, "keepAlive": "-1", "maxLoadedModels": 2,
                "flashAttention": True, "kvCacheType": "q8_0", "tailscaleServe": True,
            },
            ollama,
        )
        self.assertEqual(["/opt/homebrew/bin/ollama", "serve"], s["ollama"]["ProgramArguments"])
        self.assertEqual(
            {
                "OLLAMA_HOST": "127.0.0.1:11434", "OLLAMA_ORIGINS": "*", "OLLAMA_FLASH_ATTENTION": "1",
                "OLLAMA_KV_CACHE_TYPE": "q8_0", "OLLAMA_KEEP_ALIVE": "-1", "OLLAMA_MAX_LOADED_MODELS": "2",
                "OLLAMA_NUM_PARALLEL": "1", "OLLAMA_CONTEXT_LENGTH": "65536",
            },
            s["ollama"]["EnvironmentVariables"],
        )
        self.assertIn("--tcp=11434", s["activation"])
        self.assertTrue(studio("{ }", "config.services.open-webui.enable"))
        self.assertTrue(s["openWebUI"]["RunAtLoad"])


class OtherHosts(unittest.TestCase):
    def test_other_darwin_hosts_do_not_import_it(self):
        hosts = json.loads(nix("eval", "--json", "--impure", "--expr", f"""
          let flake = builtins.getFlake "path:{REPO}";
          in map (host: let config = flake.darwinConfigurations.${{host}}.config; in {{
            option = config.services ? omlx;
            agents = builtins.filter (lib: builtins.match ".*omlx.*" lib != null) (builtins.attrNames config.launchd.user.agents);
          }}) [ "mbp" "a6mbp" ]
        """))
        self.assertEqual([{"option": False, "agents": []}] * 2, hosts)

    def test_nixos_stub_is_a_valid_module(self):
        same = json.loads(nix("eval", "--json", "--impure", "--expr", f"""
          let
            flake = builtins.getFlake "path:{REPO}";
            gnarbox = flake.nixosConfigurations.gnarbox;
            withStub = gnarbox.extendModules {{ modules = [ flake.modules.nixos.omlx ]; }};
          in withStub.config.networking.hostName == gnarbox.config.networking.hostName
        """))
        self.assertTrue(same)


class Enabled(unittest.TestCase):
    """Enabled: one foreground `omlx serve` agent on loopback, behind the preflight."""

    def test_the_agent_runs_the_reviewed_serve_contract(self):
        # The plist launchd reads, not the option tree.
        agent = plistlib.loads(studio(ENABLED, 'config.environment.userLaunchAgents."org.nixos.omlx.plist".text').encode())
        self.assertEqual("org.nixos.omlx", agent["Label"])
        args = agent["ProgramArguments"]
        self.assertTrue(args[0].endswith("/bin/bash"), args[0])
        self.assertTrue(args[1].endswith("-omlx-preflight.sh"), args[1])
        self.assertTrue(args[2].endswith("/bin/jq"), args[2])
        self.assertEqual(
            [
                STATE,
                f"{STATE}/runtime/venv/bin/omlx",
                "serve",
                "--base-path", STATE,
                "--model-dir", f"{STATE}/models",
                "--host", "127.0.0.1",
                "--port", "8000",
                "--max-concurrent-requests", "1",
                "--memory-guard", "safe",
                "--paged-ssd-cache-dir", f"{STATE}/cache",
                "--paged-ssd-cache-max-size", "20GB",
                "--no-hf-cache",
                "--headless",
                "--log-level", "info",
            ],
            args[3:],
        )
        self.assertIs(True, agent["RunAtLoad"])
        # A crash restarts it; a refused preflight or a configuration error does not loop.
        self.assertEqual({"Crashed": True}, agent["KeepAlive"])
        self.assertEqual("/tmp/omlx.log", agent["StandardOutPath"])
        self.assertEqual("/tmp/omlx.error.log", agent["StandardErrorPath"])
        self.assertEqual(
            {"Label", "ProgramArguments", "EnvironmentVariables", "RunAtLoad", "KeepAlive",
             "StandardOutPath", "StandardErrorPath"},
            set(agent),
        )

    def test_environment_holds_no_secret_and_keeps_hugging_face_state_private(self):
        env = studio(ENABLED, f"{AGENT}.EnvironmentVariables")
        self.assertEqual(
            {
                "HOME": HOME,
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HF_HOME": f"{STATE}/huggingface",
                "HF_HUB_OFFLINE": "1",
                "HF_HUB_DISABLE_TELEMETRY": "1",
                "TRANSFORMERS_OFFLINE": "1",
            },
            env,
        )

    def test_no_network_exposure_tools_or_credentials_on_the_command_line(self):
        args = studio(ENABLED, f"{AGENT}.ProgramArguments")
        for flag in ("--api-key", "--mcp-config", "--hf-endpoint", "--ms-endpoint", "--http-proxy",
                     "--https-proxy", "--ca-bundle", "--memory-guard-gb", "--no-cache"):
            self.assertNotIn(flag, args)
        self.assertNotIn("0.0.0.0", args)
        self.assertNotIn("auto", args)

    def test_enabling_it_changes_nothing_else(self):
        off = studio("{ }", SUMMARY)
        on = studio(ENABLED, SUMMARY)
        self.assertEqual([], on["failed"])
        self.assertEqual(["omlx"], [name for name in on["agents"] if "omlx" in name])
        self.assertEqual(sorted(off["agents"] + ["omlx"]), sorted(on["agents"]))
        self.assertEqual(off["system"], on["system"])
        # No firewall rule, Serve route, download, install or restart at activation.
        self.assertEqual(off["activation"], on["activation"])
        for key in ("taps", "brews", "casks", "packages", "ollama", "openWebUI"):
            self.assertEqual(off[key], on[key], key)

    def test_settings_flow_through(self):
        args = studio(
            '{ enable = lib.mkForce true; port = lib.mkForce 18000; stateDir = lib.mkForce "/Users/bryan/omlx-state";'
            ' memoryGuard = lib.mkForce "balanced"; ssdCacheGB = lib.mkForce 8; }',
            f"{AGENT}.ProgramArguments",
        )
        self.assertEqual("/Users/bryan/omlx-state/runtime/venv/bin/omlx", args[4])
        for flag, value in (("--port", "18000"), ("--memory-guard", "balanced"),
                            ("--paged-ssd-cache-max-size", "8GB"), ("--base-path", "/Users/bryan/omlx-state"),
                            ("--model-dir", "/Users/bryan/omlx-state/models"),
                            ("--paged-ssd-cache-dir", "/Users/bryan/omlx-state/cache")):
            self.assertEqual(value, args[args.index(flag) + 1], flag)


def failures(*settings):
    """Failed assertion messages for each `services.omlx` setting, in one evaluation."""
    each = " ".join(f"({s})" for s in settings)
    return json.loads(nix("eval", "--json", "--impure", "--expr", f"""
      let
        studio = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio;
        inherit (studio.pkgs) lib;
        failed = settings: let config = (studio.extendModules {{
          modules = [ {{ services.omlx = settings; }} ];
        }}).config; in {FAILED};
      in map failed [ {each} ]
    """))


class Refusals(unittest.TestCase):
    def test_memory_guard_cannot_be_off_or_aggressive(self):
        for tier in ("off", "aggressive", "custom"):
            with self.subTest(tier=tier):
                self.assertIn("memoryGuard", refusal(f'{{ enable = lib.mkForce true; memoryGuard = lib.mkForce "{tier}"; }}'))

    def test_ssd_cache_is_a_fixed_positive_size(self):
        for value in ("0", "(-1)", '"auto"'):
            with self.subTest(value=value):
                self.assertIn("ssdCacheGB", refusal(f"{{ enable = lib.mkForce true; ssdCacheGB = lib.mkForce {value}; }}"))

    def test_port_is_unprivileged_and_not_ollamas(self):
        accepted = ["{ enable = lib.mkForce true; port = lib.mkForce 8000; }",
                    "{ enable = lib.mkForce true; port = lib.mkForce 18000; }"]
        refused = [f"{{ enable = lib.mkForce true; port = lib.mkForce {port}; }}" for port in (80, 443, 1023, 11434)]
        results = failures(*accepted, *refused)
        for settings, failed in zip(accepted, results):
            with self.subTest(accepted=settings):
                self.assertEqual([], failed)
        for settings, failed in zip(refused, results[len(accepted):]):
            with self.subTest(refused=settings):
                self.assertTrue(any("services.omlx.port" in m for m in failed), failed)

    def test_state_dir_is_a_plain_absolute_path(self):
        refused = [f"{{ enable = lib.mkForce true; stateDir = lib.mkForce {value}; }}" for value in (
            '"relative/omlx"', '"/"', '"/Users/bryan/omlx/"', '"/Users/bryan/../omlx"', '"/Users/bryan/a b"',
            r'"/Users/bryan/omlx\n--host 0.0.0.0"', '""',
        )]
        for settings, failed in zip(refused, failures(*refused)):
            with self.subTest(refused=settings):
                self.assertTrue(any("services.omlx.stateDir" in m for m in failed), failed)


@cache
def tools():
    """The bash and jq the agent runs the preflight with, realised in the local store."""
    args = studio(ENABLED, f"{AGENT}.ProgramArguments")
    nix("build", "--no-link", "--impure", "--expr",
        f'let pkgs = (builtins.getFlake "path:{REPO}").darwinConfigurations.studio.pkgs; in [ pkgs.bash pkgs.jq.bin ]')
    for path in args[0], args[2]:
        if not os.access(path, os.X_OK):
            raise AssertionError(f"{path} is not what the store just built")
    return args[0], args[2]


# What commissioning writes before the first start (docs/omlx-glm53-trial.md).
COMMISSIONED = {
    "version": "1.0",
    "server": {
        "host": "127.0.0.1", "port": 8000, "cors_origins": [], "server_aliases": ["127.0.0.1"],
        "distributed_inference_enabled": False,
    },
    "auth": {
        "api_key": "0" * 64, "secret_key": "1" * 64, "skip_api_key_verification": False,
        "allow_unauthenticated_inference": False, "sub_keys": [],
    },
    "mcp": {"config_path": None, "expose_tools": False},
    "huggingface": {"endpoint": "", "hf_cache_enabled": False},
}


def commissioned(**changes):
    """COMMISSIONED with `section__key=value` changes; a value of ... removes the key."""
    settings = json.loads(json.dumps(COMMISSIONED))
    for name, value in changes.items():
        section, key = name.split("__")
        if value is ...:
            settings[section].pop(key, None)
        else:
            settings[section][key] = value
    return settings


class Preflight(unittest.TestCase):
    """The helper the agent runs before `omlx serve`. Run here against a stand-in executable."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.exe = self.state / "runtime" / "venv" / "bin" / "omlx"
        self.record = self.root / "argv"
        self.bash, self.jq = tools()

    def tearDown(self):
        self.tmp.cleanup()

    def install_stand_in(self, settings=COMMISSIONED):
        self.exe.parent.mkdir(parents=True)
        self.exe.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{self.record}"\n')
        self.exe.chmod(self.exe.stat().st_mode | stat.S_IXUSR)
        if settings is not None:
            self.write("settings.json", settings)

    def write(self, name, value):
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def run_preflight(self):
        # No PATH: the helper may use only shell builtins and the jq it is given,
        # so it cannot download, install or start anything else.
        return subprocess.run(
            [self.bash, str(PREFLIGHT), self.jq, str(self.state), str(self.exe), "serve", "--port", "8000"],
            env={"PATH": "/nonexistent"}, text=True, capture_output=True,
        )

    def assertRuns(self):
        result = self.run_preflight()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(["serve", "--port", "8000"], self.record.read_text().splitlines())

    def assertRefused(self, phrase):
        result = self.run_preflight()
        self.assertEqual(EX_CONFIG, result.returncode, result.stderr)
        self.assertIn(phrase, result.stderr)
        self.assertIn("docs/omlx-glm53-trial.md", result.stderr)
        self.assertNotIn("0" * 32, result.stderr)  # never echoes the key
        self.assertFalse(self.record.exists())

    def test_missing_state_directory_is_refused_and_not_created(self):
        self.assertRefused("state directory")
        self.assertFalse(self.state.exists())

    def test_missing_runtime_is_refused(self):
        self.state.mkdir()
        self.assertRefused("runtime")
        self.assertEqual([], list(self.state.iterdir()))

    def test_runs_the_contract_command_unchanged_once_commissioned(self):
        self.install_stand_in()
        self.assertRuns()

    def test_uncommissioned_settings_are_refused(self):
        # oMLX's own defaults: no API key and CORS `*`, so any web page in the
        # operator's browser could claim the admin key (setup-api-key) and use /v1.
        for name, settings in {
            "missing": None,
            "unreadable": "{not json",
            "no api key": commissioned(auth__api_key=...),
            "null api key": commissioned(auth__api_key=None),
            "short api key": commissioned(auth__api_key="0" * 31),
            "default cors": commissioned(server__cors_origins=...),
            "cors *": commissioned(server__cors_origins=["*"]),
            "skip verification": commissioned(auth__skip_api_key_verification=True),
            "unauthenticated inference": commissioned(auth__allow_unauthenticated_inference=True),
            "cluster": commissioned(server__distributed_inference_enabled=True),
            "mcp": commissioned(mcp__config_path="/Users/bryan/mcp.json"),
        }.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                self.install_stand_in(settings=None)
                if settings is not None:
                    self.write("settings.json", settings)
                self.assertRefused("settings.json")

    def test_unpinned_model_settings_are_accepted(self):
        self.install_stand_in()
        self.write("model_settings.json", {"version": "1.0", "models": {"glm": {"is_pinned": False, "moe_expert_offload_enabled": True}}})
        self.assertRuns()

    def test_a_pinned_or_unreadable_model_setting_is_refused(self):
        for value in ({"version": "1.0", "models": {"small": {"is_pinned": False}, "glm": {"is_pinned": True}}}, "{not json"):
            with self.subTest(value=value):
                self.tearDown()
                self.setUp()
                self.install_stand_in()
                self.write("model_settings.json", value)
                self.assertRefused("pinned")

    def test_a_plain_checkpoint_is_accepted(self):
        self.install_stand_in()
        self.write("models/GLM-5.3-Flash/config.json", {"model_type": "glm5_next", "architectures": ["Glm5NextForConditionalGeneration"]})
        self.write("models/GLM-5.3-Flash/model-00001-of-00002.safetensors", "")
        self.assertRuns()

    def test_a_checkpoint_carrying_code_is_refused(self):
        # mlx-vlm imports config.json's model_file regardless of trust_remote_code.
        for name, files in {
            "model_file": {"config.json": {"model_type": "glm5_next", "model_file": "modeling.py"}},
            "auto_map": {"config.json": {"model_type": "glm5_next", "auto_map": {"AutoModel": "x.Y"}}},
            "python": {"config.json": {"model_type": "glm5_next"}, "modeling_glm.py": "import os\n"},
            "unreadable": {"config.json": "{not json"},
        }.items():
            with self.subTest(name):
                self.tearDown()
                self.setUp()
                self.install_stand_in()
                for filename, value in files.items():
                    self.write(f"models/suspect/{filename}", value)
                self.assertRefused("model directory")


class Runbook(unittest.TestCase):
    """docs/omlx-glm53-trial.md stays executable as written; nothing in it is run here except the seed."""

    TEXT = (REPO / "docs" / "omlx-glm53-trial.md").read_text()

    def test_every_bash_block_parses(self):
        blocks = re.findall(r"```bash\n(.*?)\n```", self.TEXT, flags=re.S)
        self.assertGreater(len(blocks), 10)
        for index, block in enumerate(blocks, 1):
            with self.subTest(block=index):
                result = subprocess.run(["/bin/bash", "-n"], input=block, text=True, capture_output=True)
                self.assertEqual(0, result.returncode, result.stderr)

    def test_isolated_builds_use_the_reviewed_hashed_build_lock(self):
        section = self.TEXT.split("**3c. Locked install", 1)[1].split("**3c'. Verify", 1)[0]
        # Join continuations so checks cover the actual command, not nearby prose.
        section = section.replace("\\\n", " ")
        compile_lines = [line for line in section.splitlines() if line.startswith("uv pip compile")]
        self.assertEqual(2, len(compile_lines))
        bootstrap, runtime = compile_lines
        self.assertIn("--only-binary :all:", bootstrap)
        self.assertIn("--generate-hashes", bootstrap)
        self.assertIn("--exclude-newer", bootstrap)
        for command in [runtime, next(line for line in section.splitlines() if line.startswith("uv pip sync"))]:
            self.assertIn('--build-constraints "$RUNTIME/build-requirements.lock"', command)
            self.assertIn("--require-build-hashes", command)
            self.assertIn('--exclude-newer "$CUTOFF"', command)
        self.assertIn("setuptools>=80", section)
        self.assertIn("setuptools-scm>=9", section)
        self.assertIn("maturin>=1,<2", section)
        # tiktoken's sdist builds with setuptools-rust, not maturin.
        self.assertIn("setuptools-rust>=1.5.2", section)
        # tokenizers' sdist keeps its macOS link flags where Cargo never finds them.
        self.assertIn('CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS="-C link-arg=-undefined -C link-arg=dynamic_lookup"', section)
        # mlx.extension runs a bare `cmake`: the kernel build must find the locked one.
        build_ext, = [line for line in section.splitlines() if "setup.py build_ext" in line]
        self.assertTrue(build_ext.lstrip().startswith('PATH="$VENV/bin:$PATH" '), build_ext)
        self.assertIn("STOP", section)

    def test_process_probes_use_the_native_server_title(self):
        # Pinned oMLX calls setproctitle("omlx-server") before serving.
        bash = "\n".join(re.findall(r"```bash\n(.*?)\n```", self.TEXT, flags=re.S))
        probes = [line for line in bash.splitlines() if "pgrep" in line]
        self.assertEqual(3, len(probes))
        for line in probes:
            self.assertIn("pgrep -x 'omlx-server'", line)
            self.assertNotIn("omlx serve", line)

    def test_the_commissioning_seed_satisfies_the_preflight(self):
        seed, = [m.group(1) for m in re.finditer(r"<<'PY'\n(.*?)\nPY\n", self.TEXT, flags=re.S)
                 if '"api_key": secrets.token_hex' in m.group(1)]
        bash, jq = tools()
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            exe = state / "runtime" / "venv" / "bin" / "omlx"
            exe.parent.mkdir(parents=True)
            exe.write_text("#!/bin/sh\nexit 0\n")
            exe.chmod(0o700)
            subprocess.run(["/usr/bin/python3", "-I", "-", str(state)], input=seed, text=True, check=True, capture_output=True)
            self.assertEqual(0o600, stat.S_IMODE((state / "settings.json").stat().st_mode))
            # It never overwrites an existing settings.json.
            again = subprocess.run(["/usr/bin/python3", "-I", "-", str(state)], input=seed, text=True, capture_output=True)
            self.assertNotEqual(0, again.returncode)
            result = subprocess.run([bash, str(PREFLIGHT), jq, str(state), str(exe), "serve"],
                                    env={"PATH": "/nonexistent"}, text=True, capture_output=True)
            self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
