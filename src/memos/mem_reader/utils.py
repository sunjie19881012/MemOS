import json
import re
import threading

from memos import log

logger = log.get_logger(__name__)

# Lazy tiktoken initialization — avoids blocking import on network download.
# The first call to count_tokens_text() triggers a background init attempt;
# if tiktoken downloading hangs (no proxy / no cache), we fall back to heuristic.
# 失败后不标记 _ENC_READY,后续调用可重试(网络恢复后自动生效)。
_ENC = None
_ENC_LOCK = threading.Lock()
_ENC_READY = False


def _get_encoding():
    global _ENC, _ENC_READY
    if _ENC_READY:
        return _ENC
    with _ENC_LOCK:
        if _ENC_READY:
            return _ENC
        _do_init()
        return _ENC


def _do_init():
    """Try loading tiktoken encoding with a 10s network timeout.

    成功:回写全局 _ENC 并标记 _ENC_READY=True(后续走快速路径)。
    失败/超时:_ENC 保持 None、_ENC_READY 保持 False,下次调用可重试。
    """
    global _ENC, _ENC_READY
    result = [None]
    exc = [None]

    def _work():
        try:
            import tiktoken

            try:
                result[0] = tiktoken.encoding_for_model("gpt-4o-mini")
            except Exception:
                result[0] = tiktoken.get_encoding("cl100k_base")
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=_work, daemon=True)
    t.start()
    t.join(timeout=10)
    if t.is_alive():
        logger.warning(
            "tiktoken init timed out (10s) — network unavailable? using heuristic fallback"
        )
        return
    if exc[0] is not None:
        logger.warning(f"tiktoken init failed: {exc[0]}, using heuristic fallback")
        return
    _ENC = result[0]
    _ENC_READY = True


def count_tokens_text(s: str) -> int:
    enc = _get_encoding()
    if enc is not None:
        return len(enc.encode(s or "", disallowed_special=()))

    # Heuristic fallback: zh chars ~1 token, others ~1 token per ~4 chars
    if not s:
        return 0
    zh_chars = re.findall(r"[\u4e00-\u9fff]", s)
    zh = len(zh_chars)
    rest = len(s) - zh
    return zh + max(1, rest // 4)


def derive_key(text: str, max_len: int = 80) -> str:
    """default key when without LLM: first max_len words"""
    if not text:
        return ""
    sent = re.split(r"[。！？!?]\s*|\n", text.strip())[0]
    return (sent[:max_len]).strip()


def parse_json_result(response_text: str) -> dict:
    s = (response_text or "").strip()

    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, flags=re.I)
    s = (m.group(1) if m else s.replace("```", "")).strip()

    i = s.find("{")
    if i == -1:
        return {}
    s = s[i:].strip()

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    j = max(s.rfind("}"), s.rfind("]"))
    if j != -1:
        try:
            return json.loads(s[: j + 1])
        except json.JSONDecodeError:
            pass

    def _cheap_close(t: str) -> str:
        t += "}" * max(0, t.count("{") - t.count("}"))
        t += "]" * max(0, t.count("[") - t.count("]"))
        return t

    t = _cheap_close(s)
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        if "Invalid \\escape" in str(e):
            s = s.replace("\\", "\\\\")
            return json.loads(s)
        logger.warning(
            f"[JSONParse] Failed to decode JSON: {e}\nTail: Raw {response_text} \
            json: {s}"
        )
        return {}


def parse_rewritten_response(text: str) -> tuple[bool, dict[int, dict]]:
    """Parse index-keyed JSON from hallucination filter response.
    Expected shape: { "0": {"need_rewrite": bool, "rewritten": str, "reason": str}, ... }
    Returns (success, parsed_dict) with int keys.
    """
    try:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
        s = (m.group(1) if m else text).strip()
        data = json.loads(s)
    except Exception:
        return False, {}

    if not isinstance(data, dict):
        return False, {}

    result: dict[int, dict] = {}
    for k, v in data.items():
        try:
            idx = int(k)
        except Exception:
            if isinstance(k, int):
                idx = k
            else:
                continue
        if not isinstance(v, dict):
            continue
        need_rewrite = v.get("need_rewrite")
        rewritten = v.get("rewritten", "")
        reason = v.get("reason", "")
        if (
            isinstance(need_rewrite, bool)
            and isinstance(rewritten, str)
            and isinstance(reason, str)
        ):
            result[idx] = {
                "need_rewrite": need_rewrite,
                "rewritten": rewritten,
                "reason": reason,
            }

    return (len(result) > 0), result


def parse_keep_filter_response(text: str) -> tuple[bool, dict[int, dict]]:
    """Parse index-keyed JSON from keep filter response.
    Expected shape: { "0": {"keep": bool, "reason": str}, ... }
    Returns (success, parsed_dict) with int keys.
    """
    try:
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.I)
        s = (m.group(1) if m else text).strip()
        data = json.loads(s)
    except Exception:
        return False, {}

    if not isinstance(data, dict):
        return False, {}

    result: dict[int, dict] = {}
    for k, v in data.items():
        try:
            idx = int(k)
        except Exception:
            if isinstance(k, int):
                idx = k
            else:
                continue
        if not isinstance(v, dict):
            continue
        keep = v.get("keep")
        reason = v.get("reason", "")
        if isinstance(keep, bool):
            result[idx] = {
                "keep": keep,
                "reason": reason,
            }
    return (len(result) > 0), result
