// SSE 聚合、JSON 解析、tool 结构清洗（对应 server.py 的
// _parse_sse_bytes / _parse_upstream_json / _sanitize_tool_calls / _normalize_finish_reason）
// 与包内通用小工具。
package codearts

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"sort"
	"strings"
)

// NormalizeFinishReason 把 CodeArts 的结束原因转成 OpenAI 客户端能识别的值。
func NormalizeFinishReason(v any) string {
	s := strOr(v, "")
	if s == "" || s == "other" {
		return "stop"
	}
	return s
}

// ParseSSEBytes 把上游 SSE 聚合为一个 OpenAI chat.completion JSON。
// 注意：HTTP 200 也可能带业务错误（chunk.error_code/error_msg → error 对象）。
func ParseSSEBytes(raw []byte) map[string]any {
	var textParts, reasoningParts []string
	toolCalls := map[int]map[string]any{}
	result := map[string]any{
		"id":      nil,
		"object":  "chat.completion",
		"created": nil,
		"model":   nil,
		"choices": []any{map[string]any{
			"index":         0,
			"message":       map[string]any{"role": "assistant", "content": ""},
			"finish_reason": "stop",
		}},
	}
	var usage any
	lastFinish := ""

	for _, line := range bytes.Split(raw, []byte("\n")) {
		line = bytes.TrimSpace(line)
		if len(line) == 0 || !bytes.HasPrefix(line, []byte("data:")) {
			continue
		}
		payload := bytes.TrimLeft(line[5:], " ")
		if string(payload) == "[DONE]" {
			continue
		}
		var chunk map[string]any
		if json.Unmarshal(payload, &chunk) != nil {
			continue
		}
		// CodeArts 可能以 HTTP 200 返回业务错误，需要保留错误信息
		if chunk["error_code"] != nil || chunk["error_msg"] != nil {
			return map[string]any{
				"error": map[string]any{
					"message": orStr(chunk["error_msg"], "CodeArts upstream error"),
					"type":    "upstream_error",
					"code":    chunk["error_code"],
				},
			}
		}
		for _, key := range []string{"id", "created", "model"} {
			if chunk[key] != nil {
				result[key] = chunk[key]
			}
		}
		if chunk["usage"] != nil {
			usage = chunk["usage"]
		}
		choices, _ := chunk["choices"].([]any)
		for _, ci := range choices {
			choice := asMap(ci)
			if choice == nil {
				continue
			}
			if choice["finish_reason"] != nil {
				lastFinish = NormalizeFinishReason(choice["finish_reason"])
			}
			delta := asMap(choice["delta"])
			if delta == nil {
				continue
			}
			if s, ok := delta["content"].(string); ok {
				textParts = append(textParts, s)
			}
			if s, ok := delta["reasoning_content"].(string); ok {
				reasoningParts = append(reasoningParts, s)
			}
			calls, _ := delta["tool_calls"].([]any)
			for _, callAny := range calls {
				call := asMap(callAny)
				if call == nil {
					continue
				}
				idxF, _ := asFloat(call["index"])
				idx := int(idxF)
				current := toolCalls[idx]
				if current == nil {
					current = map[string]any{
						"id":       call["id"],
						"type":     orStr(call["type"], "function"),
						"function": map[string]any{"name": "", "arguments": ""},
					}
					toolCalls[idx] = current
				}
				if call["id"] != nil {
					current["id"] = call["id"]
				}
				if call["type"] != nil {
					current["type"] = call["type"]
				}
				fn := asMap(call["function"])
				curFn := asMap(current["function"])
				if fn != nil && curFn != nil {
					if fn["name"] != nil {
						curFn["name"] = strOr(curFn["name"], "") + strOr(fn["name"], "")
					}
					if fn["arguments"] != nil {
						curFn["arguments"] = strOr(curFn["arguments"], "") + strOr(fn["arguments"], "")
					}
				}
			}
		}
	}

	choice := asMap(result["choices"].([]any)[0])
	message := asMap(choice["message"])
	message["content"] = strings.Join(textParts, "")
	if len(reasoningParts) > 0 {
		message["reasoning_content"] = strings.Join(reasoningParts, "")
	}
	if len(toolCalls) > 0 {
		idxs := make([]int, 0, len(toolCalls))
		for idx := range toolCalls {
			idxs = append(idxs, idx)
		}
		sort.Ints(idxs)
		calls := make([]any, 0, len(idxs))
		for _, idx := range idxs {
			calls = append(calls, toolCalls[idx])
		}
		message["tool_calls"] = calls
	}
	if lastFinish == "" {
		if len(toolCalls) > 0 {
			lastFinish = "tool_calls"
		} else {
			lastFinish = "stop"
		}
	}
	choice["finish_reason"] = lastFinish
	if usage != nil {
		result["usage"] = usage
	}
	return result
}

// ParseUpstreamJSON 兼容上游普通 JSON 和错误地返回 SSE 的情况。
// body 为空/不可解析时返回 error 形状（调用方按业务错误处理）。
func ParseUpstreamJSON(body []byte) map[string]any {
	var result map[string]any
	if err := json.Unmarshal(body, &result); err == nil {
		if result != nil {
			choices, _ := result["choices"].([]any)
			for _, ci := range choices {
				if choice := asMap(ci); choice != nil {
					choice["finish_reason"] = NormalizeFinishReason(choice["finish_reason"])
				}
			}
		}
		return result
	}
	return ParseSSEBytes(body)
}

// SanitizeToolCalls 清洗 messages 里悬空的 tool_calls / 孤儿 tool 消息（原地改）。
// 上游对结构校验比 OpenAI 严格：assistant 带 tool_calls 后必须紧跟对应
// tool_call_id 的 tool 消息，缺一个就报 InferHub.malformed_json.400
// "insufficient tool messages following tool_calls message"。
// 客户端被中断时最容易留下悬空结构。原则：只动消息结构，不碰字段名。
// NormalizeToolChoice 上游不接受 tool_choice 对象形态（InferHub 400：
// The request param is invalid）。降级规则与官方客户端一致：有 tools 时降 "auto"，
// 没 tools 直接删键。字符串形态（auto/none/required）原样保留。
func NormalizeToolChoice(req map[string]any) {
	tc, ok := req["tool_choice"]
	if !ok || tc == nil {
		return
	}
	if _, isObj := tc.(map[string]any); !isObj {
		return
	}
	if tools, ok2 := req["tools"].([]any); ok2 && len(tools) > 0 {
		req["tool_choice"] = "auto"
		return
	}
	delete(req, "tool_choice")
}

func SanitizeToolCalls(req map[string]any) {
	msgs, ok := req["messages"].([]any)
	if !ok {
		return
	}
	// tool_call_id → 是否有对应的 tool 回复
	answered := map[string]bool{}
	for _, mi := range msgs {
		m := asMap(mi)
		if m != nil && strOr(m["role"], "") == "tool" && strOr(m["tool_call_id"], "") != "" {
			answered[strOr(m["tool_call_id"], "")] = true
		}
	}
	// tool_call_id → 对应的 assistant 调用是否存在（识别孤儿 tool 消息）
	called := map[string]bool{}
	for _, mi := range msgs {
		m := asMap(mi)
		if m == nil || strOr(m["role"], "") != "assistant" {
			continue
		}
		for _, tcAny := range asList(m["tool_calls"]) {
			if tc := asMap(tcAny); tc != nil && strOr(tc["id"], "") != "" {
				called[strOr(tc["id"], "")] = true
			}
		}
	}

	out := make([]any, 0, len(msgs))
	droppedAssistant, strippedAssistant, droppedTool := 0, 0, 0
	for _, mi := range msgs {
		m := asMap(mi)
		if m == nil {
			out = append(out, mi)
			continue
		}
		role := strOr(m["role"], "")
		if role == "tool" {
			// 找不到对应 assistant 调用的 tool 消息：上游同样会拒
			if !called[strOr(m["tool_call_id"], "")] {
				droppedTool++
				continue
			}
			out = append(out, m)
			continue
		}
		if role == "assistant" && len(asList(m["tool_calls"])) > 0 {
			dangling := false
			for _, tcAny := range asList(m["tool_calls"]) {
				if tc := asMap(tcAny); tc != nil && !answered[strOr(tc["id"], "")] {
					dangling = true
					break
				}
			}
			if dangling {
				if s, ok := m["content"].(string); ok && s != "" {
					// 有文本：只摘掉悬空 tool_calls，保留这段 assistant 内容
					stripped := map[string]any{}
					for k, v := range m {
						if k != "tool_calls" {
							stripped[k] = v
						}
					}
					m = stripped
					strippedAssistant++
				} else {
					// 无内容：整条丢弃，否则留下一个空 assistant
					droppedAssistant++
					continue
				}
			}
		}
		out = append(out, m)
	}

	if fixed := droppedAssistant + strippedAssistant + droppedTool; fixed > 0 {
		req["messages"] = out
	}
}

// marshalCompact 紧凑 JSON，不转义 HTML（对齐 json.dumps(ensure_ascii=False)）。
func marshalCompact(v any) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return []byte("null")
	}
	return bytes.TrimRight(buf.Bytes(), "\n")
}

// ── 小工具 ───────────────────────────────────────────────────────────────────

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

func asFloat(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case int:
		return float64(t), true
	}
	return 0, false
}

func strOr(v any, def string) string {
	if s, ok := v.(string); ok {
		return s
	}
	return def
}

func orStr(v any, def string) string {
	// Python `a or b` 语义：nil/空字符串都回退
	if s, ok := v.(string); ok {
		if s != "" {
			return s
		}
		return def
	}
	if v != nil {
		return fmt.Sprint(v)
	}
	return def
}

func truthy(v any) bool {
	b, _ := v.(bool)
	return b
}

func truthyOr(v any, def bool) bool {
	if b, ok := v.(bool); ok {
		return b
	}
	return def
}

func orMap(v any, def map[string]any) map[string]any {
	if m := asMap(v); m != nil {
		return m
	}
	return def
}

func readAllBody(r io.Reader) ([]byte, error) {
	return io.ReadAll(io.LimitReader(r, 16<<20))
}

func errString(s string) error { return fmt.Errorf("%s", s) }
