"""Claude Converse adapter. The agent keeps one provider-neutral tool transcript.

AWS event-stream frames are bounded and CRC checked before any event is used.
Only a complete message with a successful stop reason can authorize tools.
"""
from __future__ import annotations

import json
import base64
import struct
import zlib

from .common import AssistantError, canonical

MAX_BYTES = 8_000_000


def request_payload(items, instructions, tools, max_tokens):
    messages = []

    def append(role, content):
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(content)
        else:
            messages.append({"role": role, "content": content})

    for item in items:
        kind = item.get("type")
        if kind == "function_call":
            arguments = json.loads(item["arguments"])
            if not isinstance(arguments, dict): raise AssistantError("MODEL_TOOL_CALL_INVALID")
            append("assistant", [{"toolUse": {"toolUseId": item["call_id"], "name": item["name"], "input": arguments}}])
        elif kind == "function_call_output":
            append("user", [{"toolResult": {"toolUseId": item["call_id"], "content": [{"text": item["output"]}]}}])
        elif kind == "reasoning":
            # Opaque reasoning from another protocol must never be forwarded.
            if "bedrock_content" in item: append("assistant", [item["bedrock_content"]])
        elif item.get("role") in ("user", "assistant"):
            content = item.get("content", "")
            if isinstance(content, str):
                blocks = [{"text": content}] if content else []
            elif isinstance(content, list):
                blocks = [{"text": x["text"]} for x in content if x.get("type") in ("input_text", "output_text") and x.get("text")]
            else:
                raise AssistantError("MODEL_INPUT_INVALID")
            if blocks: append(item["role"], blocks)
        else:
            raise AssistantError("MODEL_INPUT_INVALID")
    payload = {"messages": messages, "system": [{"text": instructions}], "inferenceConfig": {"maxTokens": max_tokens}}
    if tools:
        payload["toolConfig"] = {"tools": [{"toolSpec": {"name": t["name"], "description": t["description"], "inputSchema": {"json": t["parameters"]}}} for t in tools]}
    # OpenAI reasoning effort/encrypted_reasoning are intentionally not mapped to
    # Claude extended thinking: they are different, model-specific contracts.
    return payload


def normalize(response):
    if not isinstance(response, dict): raise AssistantError("MODEL_RESPONSE_INVALID")
    reason = response.get("stopReason")
    if reason in ("guardrail_intervened", "content_filtered", "refusal"):
        raise AssistantError("MODEL_REFUSAL")
    if reason not in ("end_turn", "tool_use", "stop_sequence"):
        raise AssistantError("MODEL_RESPONSE_INCOMPLETE", "推論が未完了です。ツールは実行していません")
    out = response.get("output")
    if not isinstance(out, dict) or not isinstance(out.get("message"), dict): raise AssistantError("MODEL_RESPONSE_INVALID")
    message = out["message"]
    if message.get("role") != "assistant" or not isinstance(message.get("content"), list):
        raise AssistantError("MODEL_RESPONSE_INVALID")
    output, calls, ids = [], 0, set()
    for block in message["content"]:
        if not isinstance(block, dict) or len(block) != 1: raise AssistantError("MODEL_RESPONSE_INVALID")
        if "text" in block and isinstance(block["text"], str):
            output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": block["text"]}]})
        elif "toolUse" in block:
            tool = block["toolUse"]
            if not isinstance(tool, dict) or not all(isinstance(tool.get(k), str) and tool[k] for k in ("toolUseId", "name")) or not isinstance(tool.get("input"), dict) or tool["toolUseId"] in ids:
                raise AssistantError("MODEL_TOOL_CALL_INVALID")
            ids.add(tool["toolUseId"]); calls += 1
            output.append({"type": "function_call", "call_id": tool["toolUseId"], "name": tool["name"], "arguments": canonical(tool["input"])})
        elif "reasoningContent" in block and isinstance(block["reasoningContent"], dict):
            output.append({"type": "reasoning", "bedrock_content": block})
        else:
            raise AssistantError("MODEL_RESPONSE_INVALID")
    if (reason == "tool_use") != bool(calls): raise AssistantError("MODEL_TOOL_CALL_INVALID")
    usage = response.get("usage", {})
    if not isinstance(usage, dict) or type(usage.get("totalTokens")) is not int or usage["totalTokens"] < 0:
        raise AssistantError("MODEL_RESPONSE_INVALID")
    return {"status": "completed", "output": output, "usage": {"total_tokens": usage["totalTokens"]}}


def _exact(stream, count):
    result = bytearray()
    while len(result) < count:
        part = stream.read(count - len(result))
        if not part: raise AssistantError("MODEL_STREAM_TRUNCATED")
        result.extend(part)
    return bytes(result)


def _events(stream):
    size = 0
    while True:
        first = stream.read(1)
        if not first: return
        prelude = first + _exact(stream, 11)
        length, header_size, crc = struct.unpack(">III", prelude)
        size += length
        if length < 16 or length > MAX_BYTES or size > MAX_BYTES or header_size > length - 16:
            raise AssistantError("MODEL_RESPONSE_TOO_LARGE")
        if zlib.crc32(prelude[:8]) & 0xffffffff != crc: raise AssistantError("MODEL_STREAM_INVALID")
        tail = _exact(stream, length - 12)
        if zlib.crc32(prelude + tail[:-4]) & 0xffffffff != struct.unpack(">I", tail[-4:])[0]:
            raise AssistantError("MODEL_STREAM_INVALID")
        raw, headers, pos = tail[:header_size], {}, 0
        while pos < len(raw):
            name_size = raw[pos]; pos += 1
            if pos + name_size + 3 > len(raw): raise AssistantError("MODEL_STREAM_INVALID")
            name = raw[pos:pos+name_size].decode("utf-8"); pos += name_size
            if raw[pos] != 7: raise AssistantError("MODEL_STREAM_INVALID")
            value_size = struct.unpack(">H", raw[pos+1:pos+3])[0]; pos += 3
            if pos + value_size > len(raw) or name in headers: raise AssistantError("MODEL_STREAM_INVALID")
            headers[name] = raw[pos:pos+value_size].decode("utf-8"); pos += value_size
        if headers.get(":message-type") != "event": raise AssistantError("MODEL_STREAM_FAILED")
        data = json.loads(tail[header_size:-4].decode("utf-8"))
        if not isinstance(data, dict): raise AssistantError("MODEL_STREAM_INVALID")
        yield headers.get(":event-type"), data


def read_stream(stream, on_delta=None):
    blocks, stopped, message_started, reason, usage = {}, set(), False, None, None
    for event, data in _events(stream):
        if event == "messageStart":
            if message_started or data.get("role") != "assistant": raise AssistantError("MODEL_STREAM_INVALID")
            message_started = True
        elif event in ("contentBlockStart", "contentBlockDelta", "contentBlockStop"):
            if not message_started or reason is not None: raise AssistantError("MODEL_STREAM_INVALID")
            index = data.get("contentBlockIndex")
            if type(index) is not int or not 0 <= index < 1024 or index in stopped: raise AssistantError("MODEL_STREAM_INVALID")
            if event == "contentBlockStart":
                if index in blocks: raise AssistantError("MODEL_STREAM_INVALID")
                tool = data.get("start", {}).get("toolUse")
                if not isinstance(tool, dict): raise AssistantError("MODEL_STREAM_INVALID")
                blocks[index] = {"toolUse": dict(tool, input="")}
            elif event == "contentBlockDelta":
                delta = data.get("delta", {})
                if not isinstance(delta, dict) or len(delta) != 1: raise AssistantError("MODEL_STREAM_INVALID")
                if "text" in delta and isinstance(delta["text"], str):
                    block = blocks.setdefault(index, {"text": ""})
                    if "text" not in block: raise AssistantError("MODEL_STREAM_INVALID")
                    block["text"] += delta["text"]
                    if on_delta: on_delta(delta["text"])
                elif "toolUse" in delta and isinstance(delta["toolUse"], dict) and isinstance(delta["toolUse"].get("input"), str):
                    if index not in blocks or "toolUse" not in blocks[index]: raise AssistantError("MODEL_STREAM_INVALID")
                    blocks[index]["toolUse"]["input"] += delta["toolUse"]["input"]
                elif "reasoningContent" in delta and isinstance(delta["reasoningContent"], dict):
                    fragment = delta["reasoningContent"]
                    block = blocks.setdefault(index, {"reasoningContent": {}})
                    if "reasoningContent" not in block or len(fragment) != 1: raise AssistantError("MODEL_STREAM_INVALID")
                    content = block["reasoningContent"]
                    if "redactedContent" in fragment and isinstance(fragment["redactedContent"], str) and "reasoningText" not in content:
                        raw = base64.b64decode(content.get("redactedContent", ""), validate=True) + base64.b64decode(fragment["redactedContent"], validate=True)
                        content["redactedContent"] = base64.b64encode(raw).decode("ascii")
                    elif "redactedContent" not in content and next(iter(fragment)) in ("text", "signature") and isinstance(next(iter(fragment.values())), str):
                        text = content.setdefault("reasoningText", {})
                        key = next(iter(fragment)); text[key] = text.get(key, "") + fragment[key]
                    else: raise AssistantError("MODEL_STREAM_INVALID")
                else:
                    raise AssistantError("MODEL_STREAM_INVALID")
            else:
                if index not in blocks: raise AssistantError("MODEL_STREAM_INVALID")
                stopped.add(index)
                if "toolUse" in blocks[index]:
                    blocks[index]["toolUse"]["input"] = json.loads(blocks[index]["toolUse"]["input"])
        elif event == "messageStop":
            if not message_started or reason is not None or set(blocks) != stopped: raise AssistantError("MODEL_STREAM_INVALID")
            reason = data.get("stopReason")
            if not isinstance(reason, str): raise AssistantError("MODEL_STREAM_INVALID")
        elif event == "metadata":
            if reason is None or usage is not None: raise AssistantError("MODEL_STREAM_INVALID")
            usage = data.get("usage")
        else:
            raise AssistantError("MODEL_STREAM_FAILED")
    if reason is None or usage is None: raise AssistantError("MODEL_STREAM_TRUNCATED")
    return normalize({"stopReason": reason, "output": {"message": {"role": "assistant", "content": [blocks[i] for i in sorted(blocks)]}}, "usage": usage})
