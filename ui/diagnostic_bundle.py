"""Privacy-preserving diagnostic bundle helpers."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path


REDACTED = "<已脱敏>"
_SENSITIVE_PARTS = (
    "path", "title", "process", "executable", "command", "hwid",
    "window", "profile", "session", "username", "user_name", "name",
    "source", "target", "trigger", "key", "modifier", "preset", "mapping",
)
_WINDOWS_PATH = re.compile(
    r"(?i)(?:[a-z]:\\|\\\\)[^\r\n\t\"']+"
)


def _sensitive_key(key):
    normalized = str(key or "").casefold()
    if normalized.endswith(("_count", "_counts")):
        return False
    return any(part in normalized for part in _SENSITIVE_PARTS)


def redact_payload(value, key="", home=None):
    if _sensitive_key(key):
        return REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): redact_payload(item_value, item_key, home)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_payload(item, key, home) for item in value]
    if isinstance(value, str):
        text = value
        if home:
            text = text.replace(str(home), "%USERPROFILE%")
            text = text.replace(str(home).replace("\\", "/"), "%USERPROFILE%")
        return _WINDOWS_PATH.sub("<本地路径>", text)
    return value


def redact_log_text(text, home=None):
    output = []
    omitted_plain_lines = 0
    for raw_line in str(text or "").splitlines():
        try:
            payload = json.loads(raw_line)
        except (TypeError, ValueError, json.JSONDecodeError):
            if raw_line.strip():
                omitted_plain_lines += 1
            continue
        output.append(json.dumps(
            redact_payload(payload, home=home),
            ensure_ascii=False,
            sort_keys=True,
        ))
    if omitted_plain_lines:
        output.append(
            f"<为保护隐私，已省略 {omitted_plain_lines} 行非结构化日志>"
        )
    return "\n".join(output) + ("\n" if output else "")


def write_diagnostic_bundle(destination, summary, config_summary, log_paths, home=None):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    included = []
    with zipfile.ZipFile(
        destination, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr(
            "summary.json",
            json.dumps(redact_payload(summary, home=home), ensure_ascii=False, indent=2),
        )
        archive.writestr(
            "configuration-summary.json",
            json.dumps(
                redact_payload(config_summary, home=home),
                ensure_ascii=False,
                indent=2,
            ),
        )
        for label, path in log_paths:
            path = Path(path)
            if not path.is_file():
                continue
            try:
                text = path.read_text("utf-8", errors="replace")
            except OSError:
                continue
            archive.writestr(f"logs/{label}", redact_log_text(text, home=home))
            included.append(label)
        archive.writestr(
            "README.txt",
            "MacroCanvas 脱敏诊断包\n\n"
            "本压缩包不包含原始配置、方案名称、快捷键、窗口标题、进程名、"
            "硬件 ID 或本地绝对路径。结构化日志中的相关字段已替换为脱敏标记，"
            "无法可靠识别的非结构化日志行会直接省略。\n"
            "提交前仍建议解压并自行复查。\n",
        )
    return included
