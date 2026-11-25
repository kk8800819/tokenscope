"""TokenScope: Claude Code/CodexのOTLP Logsを安全に可視化する教材アプリ。"""

import json
import sqlite3
import statistics
import threading
from html import escape

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from google.protobuf import json_format
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import (
    ExportLogsServiceRequest,
)


_LOCK = threading.Lock()
_DB = sqlite3.connect(":memory:", check_same_thread=False)
_DB.row_factory = sqlite3.Row
_DB.execute(
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        provider TEXT NOT NULL,
        runtime TEXT NOT NULL,
        model TEXT,
        analysis_unit TEXT NOT NULL,
        unit_kind TEXT NOT NULL,
        correlation_quality TEXT NOT NULL,
        scenario TEXT,
        variant TEXT,
        input_tokens INTEGER,
        output_tokens INTEGER,
        cache_read_tokens INTEGER,
        cache_create_tokens INTEGER,
        reasoning_tokens INTEGER,
        accounted_total_tokens INTEGER,
        token_semantics TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'main',
        run_ref TEXT,
        cost_usd REAL,
        call_contexts TEXT
    )
    """
)
_DB.commit()


def _any_value(value):
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    return None


def _attributes(items):
    result = {}
    for item in items or []:
        key = item.get("key")
        if isinstance(key, str) and key in {
            "service.name", "provider", "model", "prompt.id", "launcher_run",
            "run.id", "input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens", "reasoning_output_tokens",
            "tokenscope.scenario", "tokenscope.variant",
            "tokenscope.role", "tokenscope.run", "tokenscope.cost_usd",
            "tokenscope.calls",
        }:
            result[key] = _any_value(item.get("value"))
    return result


def _token(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _source(service_name, scope_name):
    value = f"{service_name or ''} {scope_name or ''}".lower()
    if "claude" in value or "anthropic" in value:
        return "anthropic", "claude-code"
    if "codex" in value or "openai" in value:
        return "openai", "codex"
    return "unknown", "unknown"


def _semantics(provider, attrs):
    input_tokens = _token(attrs.get("input_tokens"))
    output_tokens = _token(attrs.get("output_tokens"))
    cache_read = _token(attrs.get("cache_read_input_tokens"))
    cache_create = _token(attrs.get("cache_creation_input_tokens"))
    reasoning = _token(attrs.get("reasoning_output_tokens"))
    if provider == "anthropic":
        parts = [input_tokens, cache_read or 0, cache_create or 0]
        input_total = sum(parts) if input_tokens is not None else None
        total = input_total + output_tokens if None not in (input_total, output_tokens) else None
        return input_total, output_tokens, cache_read, cache_create, reasoning, total, "anthropic-v1"
    if provider == "openai":
        invalid_cache = cache_read is not None and (
            input_tokens is None or cache_read > input_tokens
        )
        invalid_reasoning = reasoning is not None and (
            output_tokens is None or reasoning > output_tokens
        )
        total = None if invalid_cache or invalid_reasoning or None in (
            input_tokens, output_tokens
        ) else input_tokens + output_tokens
        return input_tokens, output_tokens, cache_read, None, reasoning, total, "openai-v1"
    return input_tokens, output_tokens, cache_read, cache_create, reasoning, None, "unknown"


def _cost(value):
    """Claude Code が報告した API 料金換算(ドル)。無ければ None。"""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _contexts(value):
    """往復ごとの文脈の大きさ(JSON の数値の配列)。形が違えば None。"""
    try:
        parsed = json.loads(value) if value else None
    except (TypeError, ValueError):
        return None
    if isinstance(parsed, list) and all(isinstance(item, int) for item in parsed):
        return json.dumps(parsed)
    return None


def normalize_otlp(data):
    calls = []
    for resource_log in data.get("resourceLogs", []):
        resource_attrs = _attributes(
            resource_log.get("resource", {}).get("attributes", [])
        )
        for scope_log in resource_log.get("scopeLogs", []):
            scope_name = scope_log.get("scope", {}).get("name")
            provider, runtime = _source(resource_attrs.get("service.name"), scope_name)
            for record in scope_log.get("logRecords", []):
                attrs = {**resource_attrs, **_attributes(record.get("attributes", []))}
                if provider == "anthropic" and attrs.get("prompt.id"):
                    unit, kind, quality = str(attrs["prompt.id"]), "prompt", "exact_provider"
                elif provider == "openai" and (
                    attrs.get("launcher_run") or attrs.get("run.id")
                ):
                    unit = str(attrs.get("launcher_run") or attrs.get("run.id"))
                    kind, quality = "launcher_run", "exact_launcher"
                else:
                    continue
                values = _semantics(provider, attrs)
                calls.append({
                    "provider": provider,
                    "runtime": runtime,
                    "model": attrs.get("model"),
                    "analysis_unit": unit,
                    "unit_kind": kind,
                    "correlation_quality": quality,
                    "scenario": attrs.get("tokenscope.scenario"),
                    "variant": attrs.get("tokenscope.variant"),
                    "input_tokens": values[0],
                    "output_tokens": values[1],
                    "cache_read_tokens": values[2],
                    "cache_create_tokens": values[3],
                    "reasoning_tokens": values[4],
                    "accounted_total_tokens": values[5],
                    "token_semantics": values[6],
                    # 後編で追加: main=本体 / internal=Claude Code内部の補助 / delegate=bulk_read.py
                    "role": attrs.get("tokenscope.role") or "main",
                    "run_ref": attrs.get("tokenscope.run") or unit,
                    "cost_usd": _cost(attrs.get("tokenscope.cost_usd")),
                    "call_contexts": _contexts(attrs.get("tokenscope.calls")),
                })
    return calls


def decode_otlp(body, content_type):
    request = ExportLogsServiceRequest()
    if "json" in content_type:
        json_format.Parse(body.decode("utf-8"), request)
    elif "protobuf" in content_type or "octet-stream" in content_type:
        request.ParseFromString(body)
    else:
        raise ValueError("unsupported_content_type")
    return json_format.MessageToDict(request)


def ingest(body, content_type):
    try:
        calls = normalize_otlp(decode_otlp(body, content_type))
    except Exception:
        return {"ok": False, "ingested": 0, "error": "invalid_otlp"}
    columns = tuple(calls[0].keys()) if calls else ()
    if calls:
        placeholders = ",".join("?" for _ in columns)
        with _LOCK:
            _DB.executemany(
                f"INSERT INTO llm_calls ({','.join(columns)}) VALUES ({placeholders})",
                [[call[column] for column in columns] for call in calls],
            )
            _DB.commit()
    return {"ok": True, "ingested": len(calls)}


def _fixture(service, records):
    return {"resourceLogs": [{
        "resource": {"attributes": [{
            "key": "service.name", "value": {"stringValue": service}
        }]},
        "scopeLogs": [{"scope": {"name": service}, "logRecords": records}],
    }]}


def _record(unit_key, unit, model, input_tokens, output_tokens, variant=None, **extra):
    values = {
        unit_key: unit,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokenscope.scenario": "article-body",
        "tokenscope.variant": variant,
        **extra,
    }
    attributes = []
    for key, value in values.items():
        if value is None:
            continue
        field = "intValue" if isinstance(value, int) else "stringValue"
        attributes.append({"key": key, "value": {field: str(value)}})
    return {"attributes": attributes}


CLAUDE_FIXTURE = _fixture("claude-code", [
    _record("prompt.id", "prompt-before-a", "claude-opus-4-8", 18000, 4000,
            "before", cache_read_input_tokens=2000),
    _record("prompt.id", "prompt-before-b", "claude-opus-4-8", 17600, 4000,
            "before", cache_read_input_tokens=1800),
    _record("prompt.id", "prompt-before-c", "claude-opus-4-8", 19000, 4000,
            "before", cache_read_input_tokens=2200),
    _record("prompt.id", "prompt-after-a", "claude-opus-4-8", 11000, 3000,
            "after", cache_read_input_tokens=1600),
    _record("prompt.id", "prompt-after-b", "claude-opus-4-8", 10900, 3000,
            "after", cache_read_input_tokens=1500),
    _record("prompt.id", "prompt-after-c", "claude-opus-4-8", 11500, 3000,
            "after", cache_read_input_tokens=1700),
])
CODEX_FIXTURE = _fixture("codex", [
    _record("launcher_run", "run-codex-001", "gpt-5.6-sol", 7200, 1800,
            cache_read_input_tokens=1200, reasoning_output_tokens=600),
    _record("launcher_run", "run-codex-missing", "gpt-5.6-sol", None, None),
])


def load_fixtures():
    with _LOCK:
        _DB.execute("DELETE FROM llm_calls")
        _DB.commit()
    claude_body = json.dumps(CLAUDE_FIXTURE).encode("utf-8")
    proto = ExportLogsServiceRequest()
    json_format.ParseDict(CODEX_FIXTURE, proto)
    first = ingest(claude_body, "application/json")
    second = ingest(proto.SerializeToString(), "application/x-protobuf")
    return first["ingested"] + second["ingested"]


def overview():
    with _LOCK:
        rows = _DB.execute(
            "SELECT provider, runtime, model, analysis_unit, unit_kind, "
            "correlation_quality, scenario, variant, input_tokens, output_tokens, "
            "cache_read_tokens, reasoning_tokens, accounted_total_tokens, token_semantics, "
            "role, run_ref, cost_usd, call_contexts "
            "FROM llm_calls ORDER BY id"
        ).fetchall()
    calls = [dict(row) for row in rows]
    known = [row["accounted_total_tokens"] for row in calls
             if row["accounted_total_tokens"] is not None]
    coverage = len(known) / len(calls) if calls else 0
    return {"calls": calls, "known_total": sum(known), "known": len(known),
            "detected": len(calls), "coverage": coverage}


def bottlenecks(calls):
    groups = {}
    for call in calls:
        if call["accounted_total_tokens"] is None:
            continue
        key = (call["provider"], call["runtime"], call["model"], call["token_semantics"])
        groups.setdefault(key, []).append(call)
    ranked = []
    for key, items in groups.items():
        for rank, call in enumerate(
            sorted(items, key=lambda row: row["accounted_total_tokens"], reverse=True), 1
        ):
            ranked.append({**call, "rank": rank, "stratum": " / ".join(key)})
    return ranked


def comparisons(calls):
    """同じstratum(同じモデル・同じ役割)の中で、beforeと各改善案を比べる。"""
    groups = {}
    for call in calls:
        if call["accounted_total_tokens"] is None or not call["variant"]:
            continue
        key = (call["scenario"], call["provider"], call["runtime"],
               call["model"], call["token_semantics"], call.get("role") or "main")
        groups.setdefault(key, {}).setdefault(call["variant"], []).append(
            call["accounted_total_tokens"]
        )
    result = []
    for key, arms in groups.items():
        if not arms.get("before"):
            continue
        before = statistics.median(arms["before"])
        for variant, values in arms.items():
            if variant == "before":
                continue
            after = statistics.median(values)
            reduction = 100 * (before - after) / before if before else None
            result.append({"scenario": key[0], "variant": variant,
                           "stratum": " / ".join(key[1:]),
                           "before": before, "after": after, "reduction": reduction,
                           "before_n": len(arms["before"]), "after_n": len(values),
                           "p95": None})
    return result


def net_comparisons(calls):
    """1回の実行ごとに、本体・内部の補助・bulk_read.pyの分を足し、差し引きで比べる。

    安いモデルへ回すと本体は減るが、そのぶん安いモデルが増える。
    本体だけの削減率ではなく、増えた分も引いた「合計」で効果を確かめる。
    """
    runs = {}
    for call in calls:
        if not call["variant"] or not call["scenario"]:
            continue
        run = runs.setdefault(call["run_ref"] or call["analysis_unit"], {
            "scenario": call["scenario"], "variant": call["variant"],
            "main": 0, "internal": 0, "delegate": 0, "cost": 0.0, "unknown": False,
        })
        run["cost"] += call.get("cost_usd") or 0
        if call["accounted_total_tokens"] is None:
            run["unknown"] = True  # 欠損があれば0にせず、その回は合計から外す
            continue
        role = call.get("role") or "main"
        run[role if role in {"main", "internal", "delegate"} else "internal"] += (
            call["accounted_total_tokens"]
        )
    arms = {}
    for run in runs.values():
        if run["unknown"]:
            continue
        run["total"] = run["main"] + run["internal"] + run["delegate"]
        arms.setdefault(run["scenario"], {}).setdefault(run["variant"], []).append(run)
    result = []
    for scenario, variants in arms.items():
        before = variants.get("before")
        before_total = statistics.median([run["total"] for run in before]) if before else None
        for variant, items in sorted(variants.items(), key=lambda item: item[0] != "before"):
            total = statistics.median([run["total"] for run in items])
            result.append({
                "scenario": scenario, "variant": variant, "n": len(items),
                "main": statistics.median([run["main"] for run in items]),
                "internal": statistics.median([run["internal"] for run in items]),
                "delegate": statistics.median([run["delegate"] for run in items]),
                "total": total,
                "cost": statistics.median([run["cost"] for run in items]),
                "reduction": (100 * (before_total - total) / before_total
                              if before_total and variant != "before" else None),
            })
    return result


def _value(value):
    return "—" if value is None else escape(f"{value:,}" if isinstance(value, int) else str(value))


def _percent(value):
    return "—" if value is None else f"{value:.1f}%"


COLORS = ["#1769e0", "#e07b17", "#2a9d4b", "#b23a48", "#6b4fbb"]


def turn_series(calls):
    """場面×改善案ごとに、n往復目に送った文脈の大きさ(中央値)を並べる。

    往復の数は回によって違うので、n往復目まで続いた回だけで中央値をとる。
    """
    runs = {}
    for call in calls:
        if (call.get("role") or "main") != "main" or not call.get("call_contexts"):
            continue
        runs.setdefault(call["scenario"], {}).setdefault(call["variant"], []).append(
            json.loads(call["call_contexts"]))
    series = {}
    for scenario, variants in runs.items():
        series[scenario] = {}
        for variant, items in variants.items():
            length = max(len(item) for item in items)
            series[scenario][variant] = [
                statistics.median([item[i] for item in items if len(item) > i])
                for i in range(length)
            ]
    return series


def _chart(scenario, variants, colors):
    """1つの場面の折れ線グラフ(横＝何往復目、縦＝送った文脈のトークン数)を SVG で作る。"""
    width, height, left, right, top, bottom = 540, 230, 62, 14, 14, 34
    highest = max(max(values) for values in variants.values()) or 1
    peak = -(-highest // 20000) * 20000  # 目盛りがきりよくなるよう 2万単位で切り上げる
    turns = max(len(values) for values in variants.values())

    def x(index):
        return left + (width - left - right) * index / max(turns - 1, 1)

    def y(value):
        return top + (height - top - bottom) * (1 - value / peak)

    parts = [f"<svg viewBox='0 0 {width} {height}' class='chart' role='img' "
             f"aria-label='{escape(scenario)}の往復ごとの文脈'>"]
    for step in range(5):  # 縦の目盛り(0〜最大を4等分)
        value = peak * step / 4
        parts.append(f"<line x1='{left}' x2='{width - right}' y1='{y(value):.1f}' "
                     f"y2='{y(value):.1f}' stroke='#e1e7f0'/><text x='{left - 6}' "
                     f"y='{y(value) + 4:.1f}' text-anchor='end'>{value / 1000:,.0f}k</text>")
    for index in range(turns):  # 横の目盛り(何往復目)
        parts.append(f"<text x='{x(index):.1f}' y='{height - 12}' "
                     f"text-anchor='middle'>{index + 1}</text>")
    for variant, values in variants.items():
        points = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(values))
        color = colors[variant]
        parts.append(f"<polyline points='{points}' fill='none' stroke='{color}' "
                     f"stroke-width='2.5'/>")
        parts.extend(f"<circle cx='{x(i):.1f}' cy='{y(v):.1f}' r='3' fill='{color}'/>"
                     for i, v in enumerate(values))
    parts.append("</svg>")
    return f"<figure><figcaption>{escape(scenario)}</figcaption>{''.join(parts)}</figure>"


def render_turn_charts(calls):
    series = turn_series(calls)
    if not series:
        return "<p class='note'>往復ごとの記録がありません。</p>"
    names = sorted({variant for variants in series.values() for variant in variants},
                   key=lambda name: (name != "before", name))
    colors = {name: COLORS[index % len(COLORS)] for index, name in enumerate(names)}
    legend = "".join(f"<span><i style='background:{colors[name]}'></i>{escape(name)}</span>"
                     for name in names)
    charts = "".join(_chart(scenario, {name: series[scenario][name] for name in names
                                       if name in series[scenario]}, colors)
                     for scenario in series)
    return f"<div class='legend'>{legend}</div><div class='charts'>{charts}</div>"


def render_page():
    data = overview()
    coverage = f"{data['known']} / {data['detected']} ({data['coverage']:.0%})"
    rank_rows = "".join(
        f"<tr><td>{row['rank']}</td><td>{escape(row['stratum'])}</td>"
        f"<td>{escape(row['analysis_unit'])}</td><td>{row['unit_kind']}</td>"
        f"<td>{row['correlation_quality']}</td><td class='num'>{row['accounted_total_tokens']:,}</td></tr>"
        for row in bottlenecks(data["calls"])
    )
    comparison_rows = "".join(
        f"<tr><td>{escape(row['scenario'])}</td><td>{escape(row['variant'])}</td>"
        f"<td>{escape(row['stratum'])}</td>"
        f"<td>{row['before']:,.0f} (n={row['before_n']})</td>"
        f"<td>{row['after']:,.0f} (n={row['after_n']})</td>"
        f"<td>{row['reduction']:.1f}%</td><td>N/A (n&lt;20)</td></tr>"
        for row in comparisons(data["calls"])
    )
    net_rows = "".join(
        f"<tr><td>{escape(row['scenario'])}</td><td>{escape(row['variant'])} (n={row['n']})</td>"
        f"<td class='num'>{row['main']:,.0f}</td><td class='num'>{row['internal']:,.0f}</td>"
        f"<td class='num'>{row['delegate']:,.0f}</td><td class='num'><b>{row['total']:,.0f}</b></td>"
        f"<td class='num'>{_percent(row['reduction'])}</td>"
        f"<td class='num'>${row['cost']:.3f}</td></tr>"
        for row in net_comparisons(data["calls"])
    )
    detail_rows = "".join(
        f"<tr><td>{row['runtime']}</td><td>{escape(row['analysis_unit'])}</td>"
        f"<td>{row['unit_kind']} / {row['correlation_quality']}</td>"
        f"<td class='num'>{_value(row['input_tokens'])}</td>"
        f"<td class='num'>{_value(row['output_tokens'])}</td>"
        f"<td class='num'>{_value(row['cache_read_tokens'])}</td>"
        f"<td class='num'>{_value(row['reasoning_tokens'])}</td>"
        f"<td class='num'>{_value(row['accounted_total_tokens'])}</td></tr>"
        for row in data["calls"]
    )
    empty = "<p class='empty'>fixtureを投入すると分析結果が表示されます。</p>"
    return PAGE.format(
        total=f"{data['known_total']:,}", coverage=coverage,
        rank_rows=rank_rows or "<tr><td colspan='6'>未集計</td></tr>",
        comparison_rows=comparison_rows or "<tr><td colspan='7'>比較データなし</td></tr>",
        net_rows=net_rows or "<tr><td colspan='8'>比較データなし</td></tr>",
        turn_charts=render_turn_charts(data["calls"]),
        detail_rows=detail_rows or "<tr><td colspan='8'>未集計</td></tr>",
        empty=empty if not data["calls"] else "",
    )


PAGE = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<title>TokenScope</title><style>
body{{font-family:system-ui,sans-serif;margin:0;background:#f4f7fb;color:#14213d}}
main{{max-width:1120px;margin:auto;padding:28px}}header{{display:flex;justify-content:space-between;align-items:center}}
h1{{margin:0;font-size:28px}}h2{{margin-top:28px;font-size:20px}}button{{background:#1769e0;color:white;border:0;border-radius:6px;padding:10px 18px;font-weight:700}}
.stats{{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;margin-top:20px}}.card{{background:white;border:1px solid #d8e1ee;border-radius:10px;padding:18px}}
.label{{color:#60708a;font-size:13px}}.metric{{font-size:25px;font-weight:800;margin-top:4px}}.note{{color:#60708a;font-size:13px}}table{{width:100%;border-collapse:collapse;background:white;font-size:13px}}
th,td{{border-bottom:1px solid #e1e7f0;padding:9px;text-align:left}}th{{background:#eaf1fb}}td.num{{text-align:right}}.empty{{padding:12px;background:#fff7db;border-left:4px solid #d49a00}}
.charts{{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px}}figure{{margin:0;background:white;border:1px solid #d8e1ee;border-radius:10px;padding:10px}}
figcaption{{font-weight:700;font-size:14px;margin-bottom:4px}}.chart{{width:100%;height:auto}}.chart text{{font-size:11px;fill:#60708a}}
.legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin:6px 0 10px}}.legend i{{display:inline-block;width:14px;height:4px;margin-right:6px;vertical-align:middle}}
</style></head><body><main><header><div><h1>TokenScope</h1><p>Claude Code / Codexのトークンを、取得確度とcoverage付きで確認します。</p></div>
<form method="post" action="/fixtures"><button id="load-fixtures" type="submit">JSON＋Protobuf fixtureを投入</button></form></header>{empty}
<div class="stats"><div class="card"><div class="label">観測できたトークン合計</div><div id="known-total" class="metric">{total}</div><div class="note">provider別の包含規則を反映</div></div>
<div class="card"><div class="label">field coverage</div><div id="coverage" class="metric">{coverage}</div><div class="note">未着・欠損は0にせずNULL</div></div></div>
<h2 id="bottlenecks">ボトルネック順位</h2><table id="bottleneck-table"><thead><tr><th>順位</th><th>同一stratum</th><th>分析単位</th><th>粒度</th><th>相関品質</th><th>tokens</th></tr></thead><tbody>{rank_rows}</tbody></table>
<h2 id="comparison">改善前後の中央値比較</h2><table id="comparison-table"><thead><tr><th>scenario</th><th>改善案</th><th>同一stratum</th><th>before中央値</th><th>after中央値</th><th>削減率</th><th>p95</th></tr></thead><tbody>{comparison_rows}</tbody></table>
<h2 id="net">差し引きで見た1回あたりのトークン(中央値)</h2><p class="note">本体＝指示を受けたモデル、内部＝Claude Codeが内部で使った補助のモデル、bulk_read＝要点を読ませた安いモデル。削減率は合計で比べます。</p><table id="net-table"><thead><tr><th>scenario</th><th>改善案</th><th>本体</th><th>内部</th><th>bulk_read</th><th>合計</th><th>beforeとの差</th><th>API料金換算</th></tr></thead><tbody>{net_rows}</tbody></table>
<p class="note">API料金換算はClaude Codeが報告した参考値です。サブスクリプションの使用量そのものではありません。キャッシュ読みは単価が安いため、トークン数と同じ割合では動きません。</p>
<h2 id="turns">往復ごとの文脈(中央値)</h2><p class="note">横は何往復目か、縦はその往復で送った文脈(入力＋キャッシュ読み＋キャッシュ作成)のトークン数。1往復目の高さが「毎回乗る文脈」です。</p>{turn_charts}
<h2 id="details">取得明細</h2><table id="detail-table"><thead><tr><th>runtime</th><th>分析単位</th><th>粒度 / 確度</th><th>input</th><th>output</th><th>cache read</th><th>reasoning</th><th>accounted total</th></tr></thead><tbody>{detail_rows}</tbody></table>
</main></body></html>"""


app = FastAPI(title="TokenScope")
ROUTES = ["/", "/fixtures", "/v1/logs", "/api/overview"]


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(render_page())


@app.post("/fixtures")
def fixtures():
    load_fixtures()
    return RedirectResponse("/#bottlenecks", status_code=303)


@app.post("/v1/logs")
async def otlp_logs(request: Request):
    result = ingest(await request.body(), request.headers.get("content-type", ""))
    return JSONResponse(result, status_code=200 if result["ok"] else 400)


@app.get("/api/overview")
def api_overview():
    data = overview()
    data["bottlenecks"] = bottlenecks(data["calls"])
    data["comparisons"] = comparisons(data["calls"])
    data["net_comparisons"] = net_comparisons(data["calls"])
    return data
