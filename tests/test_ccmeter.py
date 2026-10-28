import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import ccmeter  # noqa: E402

NOW = datetime.now(timezone.utc)


def read_json(path):
    with open(path) as f:
        return json.load(f)


def record(i, t, model="claude-opus-5-5", out=1000, session="s1", speed=None, **usage):
    u = {"input_tokens": 100, "output_tokens": out, "cache_read_input_tokens": 0,
         "cache_creation_input_tokens": 0, **usage}
    if speed:
        u["speed"] = speed
    return {"timestamp": t.isoformat().replace("+00:00", "Z"), "sessionId": session, "requestId": f"r{i}",
            "message": {"id": f"m{i}", "model": model, "usage": u}}


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.proj = os.path.join(root, "claude", "projects", "-proj")
        os.makedirs(self.proj)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": os.path.join(root, "claude"),
                                                "XDG_CACHE_HOME": os.path.join(root, "cache"),
                                                "XDG_CONFIG_HOME": os.path.join(root, "config")})
        self.env.start()
        self.log = os.path.join(self.proj, "a.jsonl")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write(self, rows, mode="w"):
        with open(self.log, mode) as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def run_cli(self, *argv, stdin=None):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(sys, "stdin", io.StringIO(stdin or "")):
            ccmeter.main(list(argv))
        return buf.getvalue()


class TestParsing(Env):
    def test_duplicates_counted_once(self):
        r = record(1, NOW)
        self.write([r, r, r])
        self.assertEqual(len(ccmeter.load_entries()), 1)

    def test_skips_synthetic_and_garbage(self):
        self.write([record(1, NOW, model="<synthetic>")])
        with open(self.log, "a") as f:
            f.write('{"broken": "usage\n')
        self.assertEqual(ccmeter.load_entries(), [])

    def test_incremental_cache_and_partial_line(self):
        self.write([record(1, NOW)])
        self.assertEqual(len(ccmeter.load_entries()), 1)
        with open(self.log, "a") as f:  # строка ещё дописывается
            f.write(json.dumps(record(2, NOW))[:40])
        self.assertEqual(len(ccmeter.load_entries()), 1)
        with open(self.log, "a") as f:
            f.write(json.dumps(record(2, NOW))[40:] + "\n")
        self.assertEqual(len(ccmeter.load_entries()), 2)

    def test_truncated_file_is_reread(self):
        self.write([record(1, NOW), record(2, NOW)])
        ccmeter.load_entries()
        self.write([record(3, NOW)])
        self.assertEqual([e[ccmeter.K] for e in ccmeter.load_entries()], ["m3:r3"])


class TestCost(unittest.TestCase):
    def entry(self, **kw):
        r = record(1, NOW, **kw)
        return ccmeter.parse_line(json.dumps(r), "p")

    def test_opus_cost(self):
        e = self.entry(out=1_000_000)  # 100 in + 1M out
        self.assertAlmostEqual(ccmeter.cost(e, ccmeter.PRICING), 20 + 100 * 4 / 1e6)

    def test_cache_write_tiers(self):
        e = self.entry(out=0, input_tokens=0, cache_creation_input_tokens=3_000_000,
                       cache_creation={"ephemeral_5m_input_tokens": 1_000_000, "ephemeral_1h_input_tokens": 2_000_000})
        self.assertAlmostEqual(ccmeter.cost(e, ccmeter.PRICING), 4 * 1.25 + 4 * 2 * 2)

    def test_fast_mode_doubles(self):
        base = ccmeter.cost(self.entry(), ccmeter.PRICING)
        self.assertAlmostEqual(ccmeter.cost(self.entry(speed="fast"), ccmeter.PRICING), base * 2)

    def test_unknown_model(self):
        self.assertIsNone(ccmeter.cost(self.entry(model="claude-future-9"), ccmeter.PRICING))

    def test_custom_pricing_wins(self):
        tbl = ccmeter.load_pricing(None, {"pricing": {"claude-opus-5-5": [1, 1, 1]}})
        self.assertEqual(ccmeter.price_for("claude-opus-5-5", tbl), (1, 1, 1))


class TestBlocks(unittest.TestCase):
    def entries(self, times):
        return [ccmeter.parse_line(json.dumps(record(i, t)), "p") for i, t in enumerate(times)]

    def test_block_starts_on_hour_and_splits(self):
        t0 = datetime(2026, 1, 1, 10, 37, tzinfo=timezone.utc)
        es = self.entries([t0, t0 + timedelta(hours=4), t0 + timedelta(hours=5)])
        blocks = ccmeter.make_blocks(es)
        self.assertEqual(len(blocks), 2)
        self.assertEqual(datetime.fromtimestamp(blocks[0]["start"], timezone.utc).hour, 10)
        self.assertEqual(len(blocks[0]["entries"]), 2)  # 14:37 < 15:00 — то же окно

    def test_gap_starts_new_block(self):
        t0 = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(len(ccmeter.make_blocks(self.entries([t0, t0 + timedelta(days=1)]))), 2)


class TestCLI(Env):
    def test_reports_run(self):
        self.write([record(1, NOW - timedelta(days=2)), record(2, NOW)])
        for cmd in ("daily", "weekly", "monthly", "session", "blocks"):
            self.assertIn("Стоимость", self.run_cli(cmd), cmd)
        data = json.loads(self.run_cli("daily", "--json"))
        self.assertEqual(data["total"]["requests"], 2)

    def test_default_command_with_flags(self):
        self.write([record(1, NOW)])
        self.assertIn("Итого", self.run_cli("--since", "2000-01-01"))

    def test_live_projection(self):
        start = NOW - timedelta(minutes=30)
        self.write([record(i, start + timedelta(minutes=i), out=10_000) for i in range(20)])
        out = self.run_cli("live", "--once", "--limit", "250000")
        self.assertIn("лимит кончится", out)
        self.assertIn("Лимит превышен", self.run_cli("live", "--once", "--limit", "1000"))

    def test_config_limit_used(self):
        self.write([record(1, NOW)])
        os.makedirs(os.path.dirname(ccmeter.config_path()))
        with open(ccmeter.config_path(), "w") as f:
            json.dump({"limit": 2200}, f)
        self.assertIn("/ 2.2k", self.run_cli("live", "--once"))

    def test_statusline(self):
        self.write([record(1, NOW, cache_read_input_tokens=50_000)])
        out = self.run_cli("statusline", stdin=json.dumps(
            {"session_id": "s1", "transcript_path": self.log, "model": {"display_name": "Opus 5.5"}}))
        plain = ccmeter._strip(out)
        for part in ("Opus 5.5", "ctx 50.1k 5%", "сессия $", "окно", "сегодня $"):
            self.assertIn(part, plain)

    def test_statusline_survives_empty_stdin(self):
        self.assertIn("сегодня", self.run_cli("statusline", stdin=""))

    def test_install_preserves_settings(self):
        settings = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json")
        with open(settings, "w") as f:
            json.dump({"theme": "dark"}, f)
        bindir = os.path.join(self.tmp.name, "bin")
        self.run_cli("install", "--bin-dir", bindir)
        data = read_json(settings)
        self.assertEqual(data["theme"], "dark")
        self.assertTrue(data["statusLine"]["command"].endswith("ccmeter statusline"))
        self.assertTrue(os.access(os.path.join(bindir, "ccmeter"), os.X_OK))

    def test_install_keeps_foreign_statusline(self):
        settings = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "settings.json")
        with open(settings, "w") as f:
            json.dump({"statusLine": {"type": "command", "command": "other"}}, f)
        self.run_cli("install", "--bin-dir", os.path.join(self.tmp.name, "bin"))
        self.assertEqual(read_json(settings)["statusLine"]["command"], "other")


if __name__ == "__main__":
    unittest.main()
