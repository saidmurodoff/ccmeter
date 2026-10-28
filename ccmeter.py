#!/usr/bin/env python3
"""ccmeter — учёт токенов и лимитов Claude Code по локальным логам.

Читает ~/.claude/projects/**/*.jsonl (и $CLAUDE_CONFIG_DIR), без зависимостей.

Команды:
  daily | weekly | monthly   — сводка по периодам
  session                    — по сессиям
  blocks                     — по 5-часовым окнам лимита
  live                       — монитор текущего окна в реальном времени
  statusline                 — одна строка для statusLine Claude Code (JSON на stdin)
  install                    — установить в ~/.local/bin и подключить status line

Настройки по умолчанию: ~/.config/ccmeter/config.json, например
  {"limit": 2000000, "limit_usd": 50, "pricing": {"claude-x": [in, out, cache_read]}}
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

VERSION = "1.1.0"
BLOCK = timedelta(hours=5)

# $ за 1M токенов: input, output, cache read. Запись в кэш: 5m = 1.25×input, 1h = 2×input.
# Префиксы проверяются по порядку — более точные выше. Переопределяется через --pricing file.json.
PRICING = [
    ("claude-fable-5-1",   10.00, 50.00, 0.25),
    ("claude-mythos-5-1",  10.00, 50.00, 0.25),
    ("claude-fable-5",     10.00, 50.00, 1.00),
    ("claude-mythos",      10.00, 50.00, 1.00),
    ("claude-opus-5-5",     4.00, 20.00, 0.20),
    ("claude-opus-5",       5.00, 25.00, 0.50),
    ("claude-opus-4-8",     5.00, 25.00, 0.50),
    ("claude-opus-4-7",     5.00, 25.00, 0.50),
    ("claude-opus-4-6",     5.00, 25.00, 0.50),
    ("claude-opus-4-5",     5.00, 25.00, 0.50),
    ("claude-opus-4",      15.00, 75.00, 1.50),
    ("claude-sonnet-5-5",   2.00, 10.00, 0.20),
    ("claude-sonnet-5",     2.00, 10.00, 0.20),
    ("claude-sonnet-4",     3.00, 15.00, 0.30),
    ("claude-haiku-5-5",    0.10,  0.50, 0.01),
    ("claude-haiku-4-5",    1.00,  5.00, 0.10),
    ("claude-3-5-haiku",    0.80,  4.00, 0.08),
]
CONTEXT_WINDOW = [("claude-haiku-4-5", 200_000), ("claude-3", 200_000), ("claude-sonnet-4-5", 200_000)]
DEFAULT_CONTEXT = 1_000_000

# Поля записи в кэше: key, ts, model, in, out, cw5m, cw1h, cr, fast, session, project
K, TS, MODEL, IN, OUT, CW5, CW1H, CR, FAST, SESS, PROJ = range(11)


# ───────────────────────── загрузка ─────────────────────────

def config_dirs() -> list[str]:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    cands = env.split(",") if env else [os.path.expanduser("~/.claude"), os.path.expanduser("~/.config/claude")]
    seen, out = set(), []
    for c in cands:
        p = os.path.realpath(os.path.join(c.strip(), "projects"))
        if p not in seen and os.path.isdir(p):
            seen.add(p)
            out.append(p)
    return out


def cache_path() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "ccmeter", "cache-v1.json")


def parse_line(line: str, project: str):
    if '"usage"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    m = d.get("message")
    if not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
        return None
    model = m.get("model") or ""
    if not model or model.startswith("<"):  # <synthetic> и т.п.
        return None
    u = m["usage"]
    try:
        ts = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, AttributeError):
        return None
    cc = u.get("cache_creation") or {}
    cw_total = u.get("cache_creation_input_tokens") or 0
    cw1h = cc.get("ephemeral_1h_input_tokens") or 0
    cw5 = cc.get("ephemeral_5m_input_tokens")
    if cw5 is None:
        cw5 = max(cw_total - cw1h, 0)
    key = f'{m.get("id", "")}:{d.get("requestId", "")}'
    if key == ":":
        key = d.get("uuid") or f"{ts}:{model}"
    return [key, ts, model, u.get("input_tokens") or 0, u.get("output_tokens") or 0,
            cw5, cw1h, u.get("cache_read_input_tokens") or 0,
            1 if u.get("speed") == "fast" else 0, d.get("sessionId") or "", project]


def load_entries(use_cache: bool = True) -> list[list]:
    """Инкрементально читает логи: дописанные файлы дочитываются с сохранённого смещения."""
    cpath = cache_path()
    cache = {}
    if use_cache:
        try:
            with open(cpath) as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
    new_cache, changed = {}, False
    for root in config_dirs():
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            rel = os.path.relpath(path, root)
            project = rel.split(os.sep, 1)[0]
            c = cache.get(path)
            if c and c["offset"] == size:
                new_cache[path] = c
                continue
            if not c or c["offset"] > size:
                c = {"offset": 0, "entries": []}
            changed = True
            with open(path, "rb") as f:
                f.seek(c["offset"])
                chunk = f.read()
            end = chunk.rfind(b"\n") + 1  # незавершённую строку дочитаем в следующий раз
            for raw in chunk[:end].splitlines():
                e = parse_line(raw.decode("utf-8", "replace"), project)
                if e:
                    c["entries"].append(e)
            c["offset"] += end
            new_cache[path] = c
    if use_cache and (changed or len(new_cache) != len(cache)):
        try:
            os.makedirs(os.path.dirname(cpath), exist_ok=True)
            tmp = cpath + f".{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump(new_cache, f, separators=(",", ":"))
            os.replace(tmp, cpath)
        except OSError:
            pass
    # Одно API-сообщение пишется в лог несколько раз (по блоку контента) — берём последнее.
    uniq = {}
    for c in new_cache.values():
        for e in c["entries"]:
            uniq[e[K]] = e
    return sorted(uniq.values(), key=lambda e: e[TS])


# ───────────────────────── расчёты ─────────────────────────

def config_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "ccmeter", "config.json")


def load_config() -> dict:
    try:
        with open(config_path()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_pricing(path: str | None, cfg: dict | None = None):
    table = list(PRICING)
    extra = dict((cfg or {}).get("pricing") or {})
    if path:
        with open(path) as f:
            extra.update(json.load(f))  # {"model-prefix": [in, out, cache_read], ...}
    return [(k, *v) for k, v in extra.items()] + table


def price_for(model: str, table):
    for prefix, pin, pout, pcr in table:
        if model.startswith(prefix):
            return pin, pout, pcr
    return None


def cost(e, table) -> float | None:
    p = price_for(e[MODEL], table)
    if not p:
        return None
    pin, pout, pcr = p
    usd = (e[IN] * pin + e[OUT] * pout + e[CW5] * pin * 1.25 + e[CW1H] * pin * 2 + e[CR] * pcr) / 1e6
    return usd * 2 if e[FAST] else usd


def context_window(model: str) -> int:
    for prefix, n in CONTEXT_WINDOW:
        if model.startswith(prefix):
            return n
    return DEFAULT_CONTEXT


class Agg:
    __slots__ = ("inp", "out", "cw", "cr", "usd", "unpriced", "n", "models", "first", "last")

    def __init__(self):
        self.inp = self.out = self.cw = self.cr = self.n = 0
        self.usd = 0.0
        self.unpriced = False
        self.models = set()
        self.first = self.last = None

    def add(self, e, table):
        self.inp += e[IN]; self.out += e[OUT]; self.cw += e[CW5] + e[CW1H]; self.cr += e[CR]
        self.n += 1
        self.models.add(e[MODEL])
        c = cost(e, table)
        if c is None:
            self.unpriced = True
        else:
            self.usd += c
        self.first = e[TS] if self.first is None else min(self.first, e[TS])
        self.last = e[TS] if self.last is None else max(self.last, e[TS])

    @property
    def total(self):
        return self.inp + self.out + self.cw + self.cr

    @property
    def active(self):
        """Токены без чтения из кэша — ближе всего к тому, что «съедает» лимит."""
        return self.inp + self.out + self.cw

    def as_dict(self):
        return {"input": self.inp, "output": self.out, "cache_write": self.cw, "cache_read": self.cr,
                "total": self.total, "cost_usd": round(self.usd, 4), "requests": self.n,
                "models": sorted(self.models)}


def make_blocks(entries):
    """5-часовые окна как у лимита: старт = час первого сообщения, длина 5 ч."""
    blocks, cur = [], None
    for e in entries:
        t = e[TS]
        if cur is None or t >= cur["end"] or t - cur["last"] >= BLOCK.total_seconds():
            start = datetime.fromtimestamp(t, timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp()
            cur = {"start": start, "end": start + BLOCK.total_seconds(), "last": t, "entries": []}
            blocks.append(cur)
        cur["entries"].append(e)
        cur["last"] = t
    return blocks


# ───────────────────────── вывод ─────────────────────────

class C:
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

    @staticmethod
    def w(code, s):
        return f"\033[{code}m{s}\033[0m" if C.on else str(s)


def fmt_n(n: float) -> str:
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def fmt_usd(a: Agg) -> str:
    return f"${a.usd:,.2f}" + ("*" if a.unpriced else "")


def fmt_dur(sec: float) -> str:
    sec = max(int(sec), 0)
    h, m = divmod(sec // 60, 60)
    return f"{h}ч {m:02d}м" if h else f"{m}м"


def short_model(m: str) -> str:
    return m.replace("claude-", "")


def bar(frac: float, width: int = 30) -> str:
    frac = max(0.0, frac)
    filled = min(int(round(frac * width)), width)
    color = "32" if frac < 0.6 else "33" if frac < 0.85 else "31"
    return C.w(color, "█" * filled) + C.w("2", "░" * (width - filled))


def table(headers, rows, align):
    widths = [max(len(_strip(str(x))) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]

    def line(cells, bold=False):
        out = []
        for c, w, a in zip(cells, widths, align):
            s = str(c)
            pad = " " * (w - len(_strip(s)))
            out.append(pad + s if a == "r" else s + pad)
        txt = "  ".join(out)
        return C.w("1", txt) if bold else txt

    print(line(headers, True))
    print(C.w("2", "  ".join("─" * w for w in widths)))
    for r in rows:
        print(line(r, r and str(r[0]).startswith("Итого")))


def _strip(s: str) -> str:
    return re.sub(r"\033\[[0-9;]*m", "", s)


def period_key(ts: float, kind: str) -> str:
    d = datetime.fromtimestamp(ts).astimezone()
    if kind == "daily":
        return d.strftime("%Y-%m-%d")
    if kind == "monthly":
        return d.strftime("%Y-%m")
    monday = (d - timedelta(days=d.weekday())).date()
    return f"{monday} (нед.)"


def filter_entries(entries, args):
    out = entries
    if getattr(args, "since", None):
        t = datetime.strptime(args.since, "%Y-%m-%d").astimezone().timestamp()
        out = [e for e in out if e[TS] >= t]
    if getattr(args, "until", None):
        t = (datetime.strptime(args.until, "%Y-%m-%d") + timedelta(days=1)).astimezone().timestamp()
        out = [e for e in out if e[TS] < t]
    if getattr(args, "project", None):
        out = [e for e in out if args.project.lower() in e[PROJ].lower()]
    return out


def usage_cols(a: Agg):
    return [fmt_n(a.inp), fmt_n(a.out), fmt_n(a.cw), fmt_n(a.cr), fmt_n(a.total), fmt_usd(a)]


USAGE_HDR = ["Input", "Output", "Cache W", "Cache R", "Всего", "Стоимость"]


def cmd_period(entries, args, tbl):
    groups, total = defaultdict(Agg), Agg()
    for e in entries:
        groups[period_key(e[TS], args.cmd)].add(e, tbl)
        total.add(e, tbl)
    if args.json:
        print(json.dumps({"periods": {k: v.as_dict() for k, v in sorted(groups.items())},
                          "total": total.as_dict()}, ensure_ascii=False, indent=2))
        return
    rows = [[k, ", ".join(short_model(m) for m in sorted(v.models)), *usage_cols(v)]
            for k, v in sorted(groups.items())]
    rows.append(["Итого", "", *usage_cols(total)])
    table(["Период", "Модели", *USAGE_HDR], rows, "llrrrrrr")
    footnote(total)


def cmd_session(entries, args, tbl):
    groups = defaultdict(Agg)
    proj = {}
    for e in entries:
        groups[e[SESS]].add(e, tbl)
        proj[e[SESS]] = e[PROJ]
    items = sorted(groups.items(), key=lambda kv: kv[1].last)[-args.limit_rows:]
    if args.json:
        print(json.dumps([{"session": k, "project": proj[k], "last": datetime.fromtimestamp(v.last).isoformat(),
                           **v.as_dict()} for k, v in items], ensure_ascii=False, indent=2))
        return
    rows = []
    for k, v in items:
        p = proj[k].lstrip("-").replace("-", "/")
        rows.append([datetime.fromtimestamp(v.last).strftime("%m-%d %H:%M"), k[:8],
                     (p[-28:] if len(p) > 28 else p), *usage_cols(v)])
    table(["Последн.", "Сессия", "Проект", *USAGE_HDR], rows, "lllrrrrrr")
    footnote(None, any(v.unpriced for _, v in items))


def cmd_blocks(entries, args, tbl):
    now = time.time()
    blocks = make_blocks(entries)[-args.limit_rows:]
    rows, out = [], []
    for b in blocks:
        a = Agg()
        for e in b["entries"]:
            a.add(e, tbl)
        active = b["end"] > now
        start = datetime.fromtimestamp(b["start"]).strftime("%m-%d %H:%M")
        status = C.w("32", f"активно, ещё {fmt_dur(b['end'] - now)}") if active else ""
        rows.append([start, *usage_cols(a), status])
        out.append({"start": datetime.fromtimestamp(b["start"]).isoformat(),
                    "end": datetime.fromtimestamp(b["end"]).isoformat(), "active": active, **a.as_dict()})
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return
    table(["Начало окна", *USAGE_HDR, ""], rows, "lrrrrrrl")
    footnote(None)


def footnote(total, unpriced=False):
    if (total and total.unpriced) or unpriced:
        print(C.w("2", "* есть модели без цены — добавьте их через --pricing"))
    print(C.w("2", "Стоимость — эквивалент по ценам API; на подписке Pro/Max вы её не платите."))


# ───────────────────────── live ─────────────────────────

def block_state(entries, tbl, limit_tokens, limit_usd):
    now = time.time()
    blocks = make_blocks(entries)
    cur = blocks[-1] if blocks and blocks[-1]["end"] > now else None
    hist = []
    for b in (blocks[:-1] if cur else blocks):
        a = Agg()
        for e in b["entries"]:
            a.add(e, tbl)
        hist.append(a)
    a = Agg()
    if cur:
        for e in cur["entries"]:
            a.add(e, tbl)
    # Лимит по умолчанию — максимум среди прошлых окон (персональный «потолок»).
    lim_t = limit_tokens or (max((h.active for h in hist), default=0) or None)
    lim_u = limit_usd or (max((h.usd for h in hist), default=0) or None)
    return cur, a, lim_t, lim_u, now


def render_live(entries, args, tbl):
    cur, a, lim_t, lim_u, now = block_state(entries, tbl, args.limit, args.limit_usd)
    lines = [C.w("1", f"ccmeter live · {datetime.now().strftime('%H:%M:%S')}"), ""]
    if not cur:
        lines.append("Активного 5-часового окна нет — лимит свободен.")
        return lines
    elapsed = max(now - cur["start"], 60)
    left = cur["end"] - now
    start_s = datetime.fromtimestamp(cur["start"]).strftime("%H:%M")
    end_s = datetime.fromtimestamp(cur["end"]).strftime("%H:%M")
    lines.append(f"Окно {start_s}–{end_s}   сброс через {C.w('1', fmt_dur(left))}")
    lines.append(f"Время   {bar(elapsed / BLOCK.total_seconds())} {elapsed / BLOCK.total_seconds():5.0%}")
    rate_t = a.active / (elapsed / 60)
    rate_u = a.usd / (elapsed / 3600)
    if lim_t:
        f = a.active / lim_t
        lines.append(f"Токены  {bar(f)} {f:5.0%}  {fmt_n(a.active)} / {fmt_n(lim_t)}")
    else:
        lines.append(f"Токены  {fmt_n(a.active)}")
    if lim_u:
        f = a.usd / lim_u
        lines.append(f"Сумма   {bar(f)} {f:5.0%}  ${a.usd:.2f} / ${lim_u:.2f}")
    lines.append("")
    lines.append(f"Скорость: {fmt_n(rate_t)} ток/мин · ${rate_u:.2f}/ч    (+ {fmt_n(a.cr)} из кэша)")
    proj_t = a.active + rate_t * left / 60
    if lim_t:
        if a.active >= lim_t:
            lines.append(C.w("31;1", f"⚠ Лимит превышен; сброс в {end_s}"))
        elif proj_t > lim_t and rate_t > 0:
            eta = (lim_t - a.active) / rate_t * 60
            when = datetime.fromtimestamp(now + eta).strftime('%H:%M')
            lines.append(C.w("31;1", f"⚠ При текущем темпе лимит кончится ~в {when} (через {fmt_dur(eta)}), раньше сброса"))
        else:
            lines.append(C.w("32", f"✓ Прогноз к сбросу: {fmt_n(proj_t)} ({proj_t / lim_t:.0%} лимита)"))
    lines.append(f"Модели: {', '.join(short_model(m) for m in sorted(a.models))}")
    if not (args.limit or args.limit_usd):
        lines.append(C.w("2", "Лимит = максимум прошлых окон. Задайте свой: --limit 2000000 или --limit-usd 50"))
    return lines


def cmd_live(args, tbl):
    try:
        while True:
            entries = filter_entries(load_entries(not args.no_cache), args)
            out = "\n".join(render_live(entries, args, tbl))
            if args.once:
                print(out)
                return
            sys.stdout.write("\033[H\033[J" + out + "\n" + C.w("2", "\nCtrl+C — выход") + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()


# ───────────────────────── statusline ─────────────────────────

def last_context(transcript: str | None) -> tuple[int, str] | None:
    if not transcript or not os.path.exists(transcript):
        return None
    with open(transcript, "rb") as f:
        f.seek(0, 2)
        f.seek(max(f.tell() - 512_000, 0))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        e = parse_line(raw.decode("utf-8", "replace"), "")
        if e:
            d = json.loads(raw)
            if d.get("isSidechain"):
                continue
            return e[IN] + e[CW5] + e[CW1H] + e[CR], e[MODEL]
    return None


def cmd_statusline(args, tbl):
    try:
        info = json.load(sys.stdin)
    except ValueError:
        info = {}
    C.on = not os.environ.get("NO_COLOR")
    entries = load_entries(not args.no_cache)
    parts = []
    model = (info.get("model") or {}).get("display_name") or ""
    if model:
        parts.append(C.w("36", model))
    ctx = last_context(info.get("transcript_path"))
    if ctx:
        used, mid = ctx
        f = used / context_window(mid)
        col = "32" if f < 0.5 else "33" if f < 0.8 else "31"
        parts.append(C.w(col, f"ctx {fmt_n(used)} {f:.0%}"))
    sid = info.get("session_id")
    if sid:
        s = Agg()
        for e in entries:
            if e[SESS] == sid:
                s.add(e, tbl)
        if s.n:
            parts.append(f"сессия {fmt_usd(s)}")
    cur, a, lim_t, _, now = block_state(entries, tbl, args.limit, None)
    if cur:
        txt = f"окно {fmt_n(a.active)}"
        if lim_t:
            f = a.active / lim_t
            txt += " " + C.w("32" if f < 0.6 else "33" if f < 0.85 else "31", f"{f:.0%}")
        parts.append(f"{txt} · сброс {fmt_dur(cur['end'] - now)}")
    today = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    d = Agg()
    for e in entries:
        if e[TS] >= today:
            d.add(e, tbl)
    parts.append(f"сегодня {fmt_usd(d)}")
    print(C.w("2", " │ ").join(parts))


# ───────────────────────── install ─────────────────────────

def cmd_install(args):
    dest = os.path.expanduser(args.bin_dir)
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, "ccmeter")
    src = os.path.realpath(__file__)
    if src != os.path.realpath(target):
        shutil.copyfile(src, target)
    os.chmod(target, 0o755)
    print(f"✓ {target}")

    settings = os.path.expanduser(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude").split(",")[0],
                                               "settings.json"))
    data = {}
    if os.path.exists(settings):
        with open(settings) as f:
            data = json.load(f)
        shutil.copyfile(settings, settings + ".bak")
    old = (data.get("statusLine") or {}).get("command")
    if old and "ccmeter" not in old and not args.force:
        print(f"! statusLine уже задан ({old}). Перезаписать: ccmeter install --force")
    else:
        cmd = target if dest not in os.environ.get("PATH", "").split(os.pathsep) else "ccmeter"
        data["statusLine"] = {"type": "command", "command": f"{cmd} statusline", "padding": 0}
        os.makedirs(os.path.dirname(settings), exist_ok=True)
        with open(settings, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"✓ statusLine подключён в {settings}")
    if dest not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"! {dest} нет в PATH — добавьте: export PATH=\"{dest}:$PATH\"")


# ───────────────────────── main ─────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(prog="ccmeter", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="cmd")

    def common(sp):
        sp.add_argument("--since", help="с даты YYYY-MM-DD")
        sp.add_argument("--until", help="по дату YYYY-MM-DD включительно")
        sp.add_argument("--project", help="фильтр по подстроке имени проекта")
        sp.add_argument("--json", action="store_true", help="вывод в JSON")
        sp.add_argument("--pricing", help="JSON с ценами: {\"prefix\": [in, out, cache_read]}")
        sp.add_argument("--no-cache", action="store_true", help="не использовать кэш разбора логов")
        sp.add_argument("--limit", type=int, help="лимит токенов на 5-часовое окно (без cache read)")
        sp.add_argument("--limit-usd", type=float, help="лимит окна в $-эквиваленте")
        sp.add_argument("-n", "--limit-rows", type=int, default=20, help="сколько строк показать")
        return sp

    for name, h in (("daily", "по дням"), ("weekly", "по неделям"), ("monthly", "по месяцам"),
                    ("session", "по сессиям"), ("blocks", "по 5-часовым окнам")):
        common(sub.add_parser(name, help=h))
    lv = common(sub.add_parser("live", help="монитор текущего окна"))
    lv.add_argument("-i", "--interval", type=float, default=5, help="период обновления, сек")
    lv.add_argument("--once", action="store_true", help="показать один раз и выйти")
    common(sub.add_parser("statusline", help="строка для statusLine Claude Code"))
    ins = sub.add_parser("install", help="установить в ~/.local/bin и подключить status line")
    ins.add_argument("--bin-dir", default="~/.local/bin")
    ins.add_argument("--force", action="store_true", help="заменить существующий statusLine")

    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        argv = ["daily", *argv]
    args = p.parse_args(argv)
    if args.cmd == "install":
        return cmd_install(args)
    cfg = load_config()
    args.limit = args.limit or cfg.get("limit")
    args.limit_usd = args.limit_usd or cfg.get("limit_usd")
    tbl = load_pricing(args.pricing, cfg)

    if args.cmd == "statusline":
        return cmd_statusline(args, tbl)
    if args.cmd == "live":
        return cmd_live(args, tbl)
    if not config_dirs():
        sys.exit("Логи Claude Code не найдены (~/.claude/projects). Задайте CLAUDE_CONFIG_DIR.")
    entries = filter_entries(load_entries(not args.no_cache), args)
    if not entries:
        print("Нет данных за выбранный период.")
        return
    {"daily": cmd_period, "weekly": cmd_period, "monthly": cmd_period,
     "session": cmd_session, "blocks": cmd_blocks}[args.cmd](entries, args, tbl)


if __name__ == "__main__":
    main()
