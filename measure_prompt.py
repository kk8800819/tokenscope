"""既存プロンプトをClaude CodeまたはCodexで実行し、usageを記録する。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_ENDPOINT = "http://127.0.0.1:8877/v1/logs"
DEFAULT_CODEX_PACKAGE = "@openai/codex@0.146.0"


def _command(name: str) -> str:
    candidates = [f"{name}.cmd", name] if os.name == "nt" else [name]
    for candidate in candidates:
        found = shutil.which(candidate)
        if found:
            return found
    raise RuntimeError(f"コマンドが見つかりません: {name}")


def _run(command: list[str], prompt: str, cwd: Path, timeout: int,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        input=prompt,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        tail = completed.stderr[-1200:].strip()
        raise RuntimeError(f"LLMの実行に失敗しました（exit={completed.returncode}）\n{tail}")
    return completed


def _stream_result(stdout: str) -> dict[str, Any]:
    """stream-json の出力から、最後の結果と1往復ごとの文脈の大きさを取り出す。

    calls[i]["context"] は i 回目の呼び出しで送った文脈(入力+キャッシュ読み+キャッシュ作成)。
    サブエージェントの呼び出しは sidechain=True で分ける。
    """
    events = [json.loads(line) for line in stdout.splitlines() if line.strip().startswith("{")]
    result = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if result is None:
        raise RuntimeError("Claude Codeの結果(result)が見つかりません")
    calls: dict[str, dict[str, Any]] = {}
    for event in events:
        message = event.get("message") or {}
        usage = message.get("usage")
        if event.get("type") != "assistant" or not usage:
            continue
        # 1回の応答が複数の行に分かれて届くので、メッセージIDでまとめる
        calls[message.get("id") or str(len(calls))] = {
            "context": sum(int(usage.get(key) or 0) for key in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")),
            "output": int(usage.get("output_tokens") or 0),
            "model": message.get("model"),
            "sidechain": event.get("parent_tool_use_id") is not None,
        }
    init = next((event for event in events if event.get("subtype") == "init"), {})
    result["calls"] = list(calls.values())
    result["tool_count"] = len(init.get("tools") or [])
    result["claude_code_version"] = init.get("claude_code_version")
    return result


def _claude(prompt: str, cwd: Path, model: str | None, effort: str | None,
            profile: Path | None, tools: str | None = "",
            allowed_tools: list[str] | None = None,
            extra_env: dict[str, str] | None = None) -> tuple[Any, str, dict[str, int]]:
    command = [
        _command("claude"),
        "--print",
        "--output-format",
        "stream-json",  # 1往復ごとの使用量も受け取るため(最後の行が json と同じ結果)
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--no-session-persistence",
    ]
    if tools is not None:  # None のときは指定せず、Claude Code の既定のツールを使う
        command.extend(["--tools", tools])
    if allowed_tools:
        command.extend(["--allowedTools", *allowed_tools])
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--effort", effort])
    env = os.environ.copy()
    if profile is not None:
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CONFIG_DIR"):
            env.pop(name, None)
        env["CLAUDE_CONFIG_DIR"] = str(profile.resolve())
    env.update(extra_env or {})
    # ツールを使う回はファイルを何度も読むので、待ち時間を長めにとる
    completed = _run(command, prompt, cwd, timeout=300 if tools == "" else 900, env=env)
    payload = _stream_result(completed.stdout)
    if payload.get("is_error"):
        raise RuntimeError(f"Claude Codeがエラーを返しました: {payload.get('result')}")
    raw_usage = payload.get("usage") or {}
    input_tokens = int(raw_usage.get("input_tokens") or 0)
    cache_read = int(raw_usage.get("cache_read_input_tokens") or 0)
    cache_create = int(raw_usage.get("cache_creation_input_tokens") or 0)
    output_tokens = int(raw_usage.get("output_tokens") or 0)
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_read,
        "cache_write_input_tokens": cache_create,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "observed_total_tokens": input_tokens + cache_read + cache_create + output_tokens,
    }
    return payload, str(payload.get("result") or ""), usage


def _codex_command() -> list[str]:
    # Windows版デスクトップアプリ内のcodex.exeを直接起動できない環境でも、
    # 検証済みのnpmパッケージへフォールバックできるようにします。
    codex_cmd = shutil.which("codex.cmd")
    if codex_cmd:
        return [codex_cmd]
    if os.name != "nt":
        codex = shutil.which("codex")
        if codex:
            return [codex]
    return [_command("npx"), "-y", DEFAULT_CODEX_PACKAGE]


def _codex(prompt: str, cwd: Path, model: str | None, effort: str | None) -> tuple[Any, str, dict[str, int]]:
    command = [
        *_codex_command(),
        "exec",
        "-",
        "--json",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "--cd",
        str(cwd),
    ]
    if model:
        command.extend(["--model", model])
    if effort:
        command.extend(["--config", f'model_reasoning_effort="{effort}"'])
    completed = _run(command, prompt, cwd, timeout=420)
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    answer = ""
    raw_usage: dict[str, Any] = {}
    for event in events:
        item = event.get("item")
        if event.get("type") == "item.completed" and isinstance(item, dict):
            if item.get("type") == "agent_message":
                answer = str(item.get("text") or "")
        if event.get("type") == "turn.completed":
            raw_usage = event.get("usage") or {}
    if not raw_usage:
        raise RuntimeError("Codexのturn.completed.usageが見つかりません")
    input_tokens = int(raw_usage.get("input_tokens") or 0)
    output_tokens = int(raw_usage.get("output_tokens") or 0)
    usage = {
        "input_tokens": input_tokens,
        "cached_input_tokens": int(raw_usage.get("cached_input_tokens") or 0),
        "cache_write_input_tokens": int(raw_usage.get("cache_write_input_tokens") or 0),
        "output_tokens": output_tokens,
        "reasoning_output_tokens": int(raw_usage.get("reasoning_output_tokens") or 0),
        "observed_total_tokens": input_tokens + output_tokens,
    }
    return events, answer, usage


def _attribute(key: str, value: str | int) -> dict[str, Any]:
    field = "intValue" if isinstance(value, int) else "stringValue"
    return {"key": key, "value": {field: str(value)}}


def model_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """claude -p の modelUsage をモデルごとに分ける。

    本体のモデルは role=main、Claude Code が内部で使った補助のモデルは role=internal。
    本体は、usage(本体の使用量)と入力・出力の数が一致するモデルで見分ける。
    """
    usage = payload.get("usage") or {}
    rows = []
    for name, values in (payload.get("modelUsage") or {}).items():
        rows.append({
            "model": name,
            "input_tokens": int(values.get("inputTokens") or 0),
            "output_tokens": int(values.get("outputTokens") or 0),
            "cache_read_input_tokens": int(values.get("cacheReadInputTokens") or 0),
            "cache_creation_input_tokens": int(values.get("cacheCreationInputTokens") or 0),
            "cost_usd": float(values.get("costUSD") or 0),
            "role": "internal",
        })
    for row in rows:
        if (row["input_tokens"], row["output_tokens"]) == (
            int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)
        ):
            row["role"] = "main"
            break
    else:
        if rows:
            max(rows, key=lambda row: row["output_tokens"])["role"] = "main"
    return rows


def _otlp_models(run_id: str, scenario: str, variant: str,
                 rows: list[dict[str, Any]],
                 contexts: list[int] | None = None) -> dict[str, Any]:
    """モデルごとの使用量を、役割(main/internal)つきで TokenScope へ送る形にする。

    本体の記録には、往復ごとの文脈の大きさ(contexts)も JSON の文字列で載せる。
    """
    records = []
    for row in rows:
        unit = run_id if row["role"] == "main" else f"{run_id}-{row['role']}-{row['model']}"
        extra = []
        if row["role"] == "main" and contexts:
            extra.append(_attribute("tokenscope.calls", json.dumps(contexts)))
        records.append({"attributes": extra + [
            _attribute("tokenscope.cost_usd", f"{row.get('cost_usd', 0):.6f}"),
            _attribute("prompt.id", unit),
            _attribute("model", row["model"]),
            _attribute("input_tokens", row["input_tokens"]),
            _attribute("output_tokens", row["output_tokens"]),
            _attribute("cache_read_input_tokens", row["cache_read_input_tokens"]),
            _attribute("cache_creation_input_tokens", row["cache_creation_input_tokens"]),
            _attribute("tokenscope.scenario", scenario),
            _attribute("tokenscope.variant", variant),
            _attribute("tokenscope.role", row["role"]),
            _attribute("tokenscope.run", run_id),
        ]})
    return {"resourceLogs": [{
        "resource": {"attributes": [_attribute("service.name", "claude-code")]},
        "scopeLogs": [{"scope": {"name": "claude-code"}, "logRecords": records}],
    }]}


def _otlp(runtime: str, model: str, effort: str, run_id: str,
          scenario: str, variant: str, usage: dict[str, int]) -> dict[str, Any]:
    unit_key = "prompt.id" if runtime == "claude-code" else "launcher_run"
    attributes = [
        _attribute(unit_key, run_id),
        _attribute("model", model),
        _attribute("input_tokens", usage["input_tokens"]),
        _attribute("output_tokens", usage["output_tokens"]),
        _attribute("tokenscope.scenario", scenario),
        _attribute("tokenscope.variant", variant),
    ]
    if usage["cached_input_tokens"]:
        attributes.append(_attribute("cache_read_input_tokens", usage["cached_input_tokens"]))
    if runtime == "claude-code" and usage["cache_write_input_tokens"]:
        attributes.append(_attribute(
            "cache_creation_input_tokens", usage["cache_write_input_tokens"]
        ))
    if runtime == "codex" and usage["reasoning_output_tokens"]:
        attributes.append(_attribute(
            "reasoning_output_tokens", usage["reasoning_output_tokens"]
        ))
    return {
        "resourceLogs": [{
            "resource": {"attributes": [_attribute("service.name", runtime)]},
            "scopeLogs": [{
                "scope": {"name": runtime},
                "logRecords": [{"attributes": attributes}],
            }],
        }],
    }


def _post(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="prompt.txtをClaude CodeまたはCodexで実行し、usageを記録します"
    )
    parser.add_argument("runtime", choices=("claude-code", "codex"))
    parser.add_argument("prompt_file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"))
    parser.add_argument("--claude-profile", type=Path)
    parser.add_argument("--scenario", default="existing-prompt")
    parser.add_argument("--variant", default="baseline")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--cwd", type=Path, default=Path.cwd())
    parser.add_argument("--output-dir", type=Path, default=Path("measurement-output"))
    parser.add_argument(
        "--save-response",
        action="store_true",
        help="回答本文と生のCLI出力も結果JSONへ保存します",
    )
    # 後編で追加: ファイルを読む作業(ツールあり)を、フックの入/切を変えて測る
    parser.add_argument("--tools", default="",
                        help='Claude Codeに使わせるツール(例: "Read,Grep,Glob,Bash")。'
                             '"default" で既定のツールをすべて使う')
    parser.add_argument("--allowed-tools", nargs="*", default=[],
                        help='確認なしで使ってよいツール(例: Read "Bash(python bulk_read.py:*)")')
    parser.add_argument("--guard", choices=("off", "block", "delegate"),
                        help="read_guard.pyの動き(フックを入れたフォルダーで使う)")
    parser.add_argument("--repeat", type=int, default=1, help="同じ条件で続けて実行する回数")
    args = parser.parse_args()

    prompt_path = args.prompt_file.resolve()
    cwd = args.cwd.resolve()
    prompt = prompt_path.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("プロンプトファイルが空です")
    for number in range(1, args.repeat + 1):
        if args.repeat > 1:
            print(f"===== {number}/{args.repeat} 回目 =====", flush=True)
        measure_once(args, prompt_path, prompt, cwd)
    return 0


def measure_once(args: argparse.Namespace, prompt_path: Path, prompt: str,
                 cwd: Path) -> dict[str, Any]:
    runtime = args.runtime
    model = args.model or "default"
    effort = args.effort or "default"
    run_id = f"run-{runtime}-{uuid.uuid4().hex[:12]}"
    args.output_dir.mkdir(parents=True, exist_ok=True)
    delegate_log = (args.output_dir / f"{run_id}-delegate.jsonl").resolve()
    # bulk_read.py が同じ回の使用量として記録できるよう、子のプロセスへ渡す
    extra_env = {
        "TOKENSCOPE_ENDPOINT": args.endpoint,
        "TOKENSCOPE_RUN_ID": run_id,
        "TOKENSCOPE_SCENARIO": args.scenario,
        "TOKENSCOPE_VARIANT": args.variant,
        "TOKENSCOPE_DELEGATE_LOG": str(delegate_log),
        # Windows の Git Bash が "/openapi.json" のような引数を勝手にパスへ書き換えないようにする
        "MSYS_NO_PATHCONV": "1",
    }
    if args.guard:
        extra_env["READ_GUARD_MODE"] = args.guard
    started = time.monotonic()
    print(f"[1/3] {prompt_path.name}を{runtime}で実行します", flush=True)
    rows: list[dict[str, Any]] = []
    if runtime == "claude-code":
        raw, answer, usage = _claude(
            prompt, cwd, args.model, args.effort, args.claude_profile,
            None if args.tools == "default" else args.tools, args.allowed_tools, extra_env,
        )
        rows = model_rows(raw)
    else:
        raw, answer, usage = _codex(prompt, cwd, args.model, args.effort)
    elapsed_ms = round((time.monotonic() - started) * 1000)

    print("[2/3] usageを取得しました", flush=True)
    print(json.dumps(usage, ensure_ascii=False, indent=2), flush=True)
    contexts = [call["context"] for call in (raw.get("calls") or []) if not call["sidechain"]]         if isinstance(raw, dict) else []
    payload = (_otlp_models(run_id, args.scenario, args.variant, rows, contexts) if rows
               else _otlp(runtime, model, effort, run_id, args.scenario, args.variant, usage))
    ingest = _post(args.endpoint, payload)
    print(f"[3/3] 管理画面へ記録しました: {ingest}", flush=True)

    delegate = []
    if delegate_log.exists():
        delegate = [json.loads(line) for line in
                    delegate_log.read_text(encoding="utf-8").splitlines() if line.strip()]
        delegate_log.unlink()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = args.output_dir / f"{stamp}-{runtime}-{run_id[-6:]}.json"
    result = {
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "run_id": run_id,
        "scenario": args.scenario,
        "variant": args.variant,
        "guard": args.guard,
        "prompt_file_name": prompt_path.name,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "prompt_chars": len(prompt),
        "usage": usage,
        "models": rows,
        "delegate_calls": delegate,
        "num_turns": raw.get("num_turns") if isinstance(raw, dict) else None,
        "calls": raw.get("calls") if isinstance(raw, dict) else None,
        "tool_count": raw.get("tool_count") if isinstance(raw, dict) else None,
        "claude_code_version": raw.get("claude_code_version") if isinstance(raw, dict) else None,
        "permission_denials": len(raw.get("permission_denials") or [])
        if isinstance(raw, dict) else None,
        "elapsed_ms": elapsed_ms,
        "ingest": ingest,
    }
    if args.save_response:
        result["answer"] = answer
        result["raw"] = raw
    output_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"結果ファイル: {output_path.resolve()}")
    result["answer"] = answer
    return result


if __name__ == "__main__":
    sys.exit(main())
