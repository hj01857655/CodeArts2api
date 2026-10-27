// 路由实现（对应 Python routes/chat.py、routes/responses.py、routes/anthropic.py、
// server.py 的 /v1/models 与 admin/panel.py）。引擎件（签名/发送/重试/闸门/
// 模型同步）全在 codearts 包，本层只做协议形状。三条聊天路由共用 prepare：
// 模型门禁 + max_tokens 钳制（sanitize 由各路由在转换后自调）。
package handler

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"iter"
	"net/http"
	"strings"
	"time"

	"codearts2api/internal/adapters"
	"codearts2api/internal/codearts"
	"codearts2api/internal/cred"
)

// prepare 清洗后的 Chat 请求：模型同步与门禁、钳制、stream 提取。
// 失败时已写好响应，返回 false。model 只在 key 不存在时取兜底——对齐 Python
// req.get("model", supported[0])：空串照走 404。notFound 由各路由给：
// chat/responses 是 OpenAI 形状，anthropic 是 Anthropic 形状。
func (s *Server) prepare(w http.ResponseWriter, rid string, chatReq map[string]any,
	notFound func(w http.ResponseWriter, model, supported string)) (model string, stream bool, ok bool) {
	s.C.RefreshIfNeeded()
	supported := s.C.Supported()
	modelAny, present := chatReq["model"]
	model = str(modelAny)
	if !present {
		model = supported[0]
	}
	if !containsStr(supported, model) {
		// 先给缓存一次同步纠正的机会：福利模型动态投放，后台刷新还没落地时
		// 本地会误判成"不存在"直接 404
		if !s.C.ForceSyncFor(model) {
			notFound(w, model, strings.Join(s.C.Supported(), ", "))
			return "", false, false
		}
		s.Log.Info(fmt.Sprintf("[%s] 模型 %s 经强制同步后命中，继续处理请求", rid, model))
	}
	chatReq["model"] = model
	// 输出上限必须在签名前收敛：签名覆盖 body 哈希，钳制必须在重序列化之前完成
	s.C.ClampMaxTokens(chatReq, rid)
	if b, isB := chatReq["stream"].(bool); isB {
		stream = b
	}
	return model, stream, true
}

func containsStr(list []string, s string) bool {
	for _, item := range list {
		if item == s {
			return true
		}
	}
	return false
}

// openAIModelNotFound chat/responses 路由的模型 404 错误体（带当前可用列表：
// 原来只说"不在可用列表中"，得去翻 models-cache.json 才知道能填什么）。
func openAIModelNotFound(w http.ResponseWriter, model, supported string) {
	writeJSON(w, http.StatusNotFound, map[string]any{
		"error": map[string]any{
			"message": fmt.Sprintf("model '%s' 不在当前账号可用模型列表中，可用：%s", model, supported),
			"type":    "invalid_request_error",
			"param":   "model",
			"code":    "model_not_found",
		}})
}

// errObj 上游 200 业务错误提取。对齐 Python result.get("error") 的 truthy 判定：
// 空 dict 是 falsy。
func errObj(result map[string]any) map[string]any {
	if e, isM := result["error"].(map[string]any); isM && len(e) > 0 {
		return e
	}
	return nil
}

// ── /v1/chat/completions ─────────────────────────────────────────────────────

func (s *Server) chat(w http.ResponseWriter, r *http.Request) {
	rid := ridFrom(r)
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 64<<20))
	if len(raw) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": map[string]any{"message": "empty body"}})
		return
	}
	var req map[string]any
	if json.Unmarshal(raw, &req) != nil || req == nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": map[string]any{"message": "invalid JSON body"}})
		return
	}

	// 清洗悬空 tool_calls / 孤儿 tool 消息（客户端中断后上游会拒整条历史）
	codearts.SanitizeToolCalls(req)
	codearts.NormalizeToolChoice(req)
	model, wantStream, ok := s.prepare(w, rid, req, openAIModelNotFound)
	if !ok {
		return
	}
	benefitChannel := !s.C.IsAgent(model)

	if !wantStream {
		// 实测 GLM-5.2 / openpangu-2.0-flash 在 stream=false 下正文丢失而 usage
		// 照计；统一向上游要流，由聚合逻辑产出单个 JSON：对客户端仍是非流式
		req["stream"] = true
	}

	resp, ue := s.C.SendUpstream(req, model, benefitChannel, rid)
	if ue != nil {
		writeUpstreamErr(w, ue)
		return
	}
	defer resp.Body.Close()

	if wantStream {
		s.writeSSE(w, r, s.chatLines(resp, model, rid))
		return
	}
	// 非流式：无论上游返回 JSON 还是 SSE，统一聚合成单个 JSON
	s.writeChatJSON(w, resp, rid)
}

// chatLines 严格转发为 OpenAI SSE；仅对 benefit 流补齐缺失的结束原因
// （对应 routes/chat.py generate()）。
func (s *Server) chatLines(resp *http.Response, model, rid string) iter.Seq[string] {
	return func(yield func(string) bool) {
		contentType := strings.ToLower(resp.Header.Get("Content-Type"))
		isBenefit := !s.C.IsAgent(model)
		sawDone := false
		sawFinish := false

		// 上游偶尔会在请求 stream=true 时返回普通 JSON，包装成单个 SSE 帧
		if !strings.Contains(contentType, "text/event-stream") {
			raw, rerr := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
			if rerr != nil {
				// 状态码已发出，只能以错误帧收尾，别让流无声断掉
				s.emitErrorFrame(yield, rid, nil, "upstream stream interrupted: "+rerr.Error())
				return
			}
			result := codearts.ParseUpstreamJSON(raw)
			normalizeChoicesFinish(result)
			if !emitData(yield, result) {
				return
			}
			emitDone(yield)
			return
		}

		scanner := bufio.NewScanner(resp.Body)
		scanner.Buffer(make([]byte, 64<<10), 8<<20)
		var broken error
		for scanner.Scan() {
			rawLine := scanner.Bytes()
			if len(rawLine) == 0 {
				continue
			}
			if !strings.HasPrefix(string(rawLine), "data:") {
				// SSE 注释行原样转发
				if strings.HasPrefix(string(rawLine), ":") {
					if !yield(string(rawLine) + "\n\n") {
						return
					}
				}
				continue
			}
			payload := strings.TrimSpace(string(rawLine[5:]))
			if payload == "[DONE]" {
				// benefit 流需要先确认是否缺少 finish_reason（[DONE] 吞下、延迟到
				// 补块之后）；agent 流保持原来的即时转发行为，避免改变客户端状态机
				if !isBenefit {
					if !sawDone {
						if !emitDone(yield) {
							return
						}
						sawDone = true
					}
				} else {
					sawDone = true
				}
				continue
			}
			var chunk map[string]any
			if json.Unmarshal([]byte(payload), &chunk) != nil {
				continue
			}
			// 上游错误帧（如 InferHub.001001005.400 param invalid）：原样透传会被
			// Agent Runtime 当成空响应无限重试。转成 OpenAI error 帧并终止流，
			// 让客户端直接看到真实原因
			if chunk["error_code"] != nil || chunk["error_msg"] != nil {
				errCode := orStrLocal(chunk["error_code"], "")
				errText := strings.TrimSpace(errCode + " " + orStrLocal(chunk["error_msg"], "upstream error"))
				s.Log.Error(fmt.Sprintf("[%s] 流中上游错误帧: %s", rid, errText))
				s.emitErrorFrame(yield, rid, chunk["error_code"], errText)
				return
			}
			for _, ci := range asListAny(chunk["choices"]) {
				choice := asMapAny(ci)
				if choice == nil {
					continue
				}
				if reason, present := choice["finish_reason"]; present && reason != nil {
					sawFinish = true
					if str(reason) == "other" {
						choice["finish_reason"] = "stop"
					}
				}
				// DeepSeek benefit 流会发 content:null、reasoning_content 有值；
				// 部分 Agent Runtime 只接受字符串 content
				if delta := asMapAny(choice["delta"]); delta != nil && delta["content"] == nil {
					delta["content"] = ""
				}
			}
			if !emitData(yield, chunk) {
				return
			}
		}
		if scanner.Err() != nil {
			broken = scanner.Err()
		}
		// 上游把流掐断了：给客户端一个明确的错误帧。必须跳过下面的补尾逻辑，
		// 否则等于给半截回答补 finish_reason=stop 伪装成正常收尾
		if broken != nil {
			s.emitErrorFrame(yield, rid, nil, "upstream stream interrupted: "+broken.Error())
			return
		}
		// 只对免费 benefit 模型补齐完全缺失的 finish_reason；不重发任何上游 chunk
		if isBenefit && !sawFinish {
			if !emitData(yield, map[string]any{
				"id": "proxy-finish", "object": "chat.completion.chunk",
				"created": time.Now().Unix(), "model": model,
				"choices": []any{map[string]any{"index": 0, "delta": map[string]any{}, "finish_reason": "stop"}},
			}) {
				return
			}
		}
		if isBenefit {
			// benefit 流的 [DONE] 遇到时被吞了，统一延迟到补块之后补发
			emitDone(yield)
			return
		}
		// agent 分支保持原有行为：上游异常断流没发 [DONE] 时代理补一个
		if !sawDone {
			emitDone(yield)
		}
	}
}

// emitErrorFrame 发 error 帧后接 [DONE]（流已 200，只能以帧收尾别让流无声断掉）。
func (s *Server) emitErrorFrame(yield func(string) bool, rid string, code any, msg string) bool {
	s.Log.Error(fmt.Sprintf("[%s] 错误帧收尾: %s", rid, msg))
	if !emitData(yield, map[string]any{
		"error": map[string]any{"message": msg, "type": "upstream_error", "code": code},
	}) {
		return false
	}
	return emitDone(yield)
}

// writeChatJSON 非流式聚合出口（chat 路由）。
func (s *Server) writeChatJSON(w http.ResponseWriter, resp *http.Response, rid string) {
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		// 读正文时连接被掐：状态码还没发出去，可以正常报错
		s.Log.Error(fmt.Sprintf("[%s] 上游响应读取中断: %v", rid, err))
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"error": map[string]any{
				"message": "upstream stream interrupted: " + err.Error(),
				"type":    "upstream_error",
				"code":    "ReadError",
			}})
		return
	}
	result := codearts.ParseUpstreamJSON(raw)
	if e := errObj(result); e != nil {
		s.Log.Error(fmt.Sprintf("[%s] [502] 上游业务错误: %v", rid, e))
		writeJSON(w, http.StatusBadGateway, result)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// ── /v1/responses（grok-build / Codex 等原生 Responses 客户端）────────────────

func (s *Server) responses(w http.ResponseWriter, r *http.Request) {
	rid := ridFrom(r)
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 64<<20))
	if len(raw) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": map[string]any{"message": "empty body"}})
		return
	}
	var reqJSON map[string]any
	if json.Unmarshal(raw, &reqJSON) != nil || reqJSON == nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": map[string]any{"message": "invalid JSON body"}})
		return
	}
	chatReq := adapters.ResponsesToChat(reqJSON)
	if len(asListAny(chatReq["messages"])) == 0 {
		writeJSON(w, http.StatusBadRequest, map[string]any{
			"error": map[string]any{"message": "empty input"}})
		return
	}
	// 与 chat 路径同款的清洗/归一化/钳制（签名前完成）
	codearts.SanitizeToolCalls(chatReq)
	codearts.NormalizeToolChoice(chatReq)
	model, wantStream, ok := s.prepare(w, rid, chatReq, openAIModelNotFound)
	if !ok {
		return
	}
	benefitChannel := !s.C.IsAgent(model)

	if !wantStream {
		// 同 chat 路径：统一向上游要流，非流式在此聚合成单个 Responses JSON
		chatReq["stream"] = true
	}

	resp, ue := s.C.SendUpstream(chatReq, model, benefitChannel, rid)
	if ue != nil {
		writeUpstreamErr(w, ue)
		return
	}
	defer resp.Body.Close()

	if wantStream {
		isSSE := strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "text/event-stream")
		s.writeSSE(w, r, func(yield func(string) bool) {
			if isSSE {
				// 常规：Chat SSE 逐帧包成 Responses SSE
				lines, lineErr := scanLines(resp.Body)
				for frame := range adapters.ChatSSEToResponses(
					adapters.SSELines{Seq: lines, Err: lineErr}, model) {
					if !yield(string(frame)) {
						return
					}
				}
				return
			}
			// 上游偶尔 stream=true 仍回普通 JSON：聚合成单个 Chat JSON，
			// 再以最小 Responses SSE 序列发出，客户端拿到的仍是合法流
			result := s.parseUpstreamLogged(resp, rid, "[responses]")
			if e := errObj(result); e != nil {
				s.Log.Error(fmt.Sprintf("[%s] [responses] [502] 上游业务错误: %v", rid, e))
				s.emitFrames(yield, adapters.ResponseJSONToSSE(
					adapters.ErrorResponseObj(model, "upstream_error", fmt.Sprint(e))))
				return
			}
			s.emitFrames(yield, adapters.ResponseJSONToSSE(
				adapters.ChatJSONToResponses(result, model)))
		})
		return
	}

	// 非流式：聚合成单个 Responses JSON
	result := s.parseUpstreamLogged(resp, rid, "[responses]")
	if e := errObj(result); e != nil {
		s.Log.Error(fmt.Sprintf("[%s] [responses] [502] 上游业务错误: %v", rid, e))
		writeJSON(w, http.StatusBadGateway, map[string]any{
			"error": map[string]any{
				"message": fmt.Sprint(e),
				"type":    "upstream_error",
				"code":    "upstream_error",
			}})
		return
	}
	writeJSON(w, http.StatusOK, adapters.ChatJSONToResponses(result, model))
}

// parseUpstreamLogged 读上游 body 并解析。读中断记日志并返回 error 形状
// （对齐 Python try/except 读中断分支；错误体形状差异由调用方处理——chat 直接
// 写 502，responses/messages 的流式分支以帧收尾）。
func (s *Server) parseUpstreamLogged(resp *http.Response, rid, tag string) map[string]any {
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<20))
	if err != nil {
		s.Log.Error(fmt.Sprintf("[%s] %s 上游响应读取中断: %v", rid, tag, err))
		return map[string]any{"error": map[string]any{
			"message": "upstream stream interrupted: " + err.Error(),
			"type":    "upstream_error",
			"code":    "ReadError",
		}}
	}
	return codearts.ParseUpstreamJSON(raw)
}

func (s *Server) emitFrames(yield func(string) bool, seq iter.Seq[[]byte]) {
	for frame := range seq {
		if !yield(string(frame)) {
			return
		}
	}
}

// ── /v1/messages（Claude Code 等原生 Anthropic 客户端）───────────────────────

func (s *Server) anthropic(w http.ResponseWriter, r *http.Request) {
	rid := ridFrom(r)
	raw, _ := io.ReadAll(io.LimitReader(r.Body, 64<<20))
	if len(raw) == 0 {
		writeJSON(w, http.StatusBadRequest, adapters.AnthropicErrorBody("invalid_request_error", "empty body"))
		return
	}
	var reqJSON map[string]any
	if json.Unmarshal(raw, &reqJSON) != nil || reqJSON == nil {
		writeJSON(w, http.StatusBadRequest, adapters.AnthropicErrorBody("invalid_request_error", "invalid JSON body"))
		return
	}
	chatReq := adapters.AnthropicToChat(reqJSON)
	if len(asListAny(chatReq["messages"])) == 0 {
		writeJSON(w, http.StatusBadRequest, adapters.AnthropicErrorBody("invalid_request_error", "empty messages"))
		return
	}
	codearts.SanitizeToolCalls(chatReq)
	codearts.NormalizeToolChoice(chatReq)
	model, wantStream, ok := s.prepare(w, rid, chatReq, func(w http.ResponseWriter, model, supported string) {
		writeJSON(w, http.StatusNotFound, adapters.AnthropicErrorBody("invalid_request_error",
			fmt.Sprintf("model '%s' 不在当前账号可用模型列表中，可用：%s", model, supported)))
	})
	if !ok {
		return
	}
	benefitChannel := !s.C.IsAgent(model)

	if !wantStream {
		chatReq["stream"] = true
	}
	resp, ue := s.C.SendUpstream(chatReq, model, benefitChannel, rid)
	if ue != nil {
		// 与 Python 一致：_send_upstream 的错误响应直接透传
		writeUpstreamErr(w, ue)
		return
	}
	defer resp.Body.Close()

	if wantStream {
		isSSE := strings.Contains(strings.ToLower(resp.Header.Get("Content-Type")), "text/event-stream")
		s.writeSSE(w, r, func(yield func(string) bool) {
			if isSSE {
				lines, _ := scanLines(resp.Body)
				for frame := range adapters.ChatSSEToAnthropic(lines, model) {
					if !yield(string(frame)) {
						return
					}
				}
				return
			}
			result := s.parseUpstreamLogged(resp, rid, "[messages]")
			if e := errObj(result); e != nil {
				s.Log.Error(fmt.Sprintf("[%s] [messages] [502] 上游业务错误: %v", rid, e))
				yield(string(adapters.AnthropicErrorSSE("api_error", fmt.Sprint(e))))
				return
			}
			s.emitFrames(yield, adapters.ChatJSONToAnthropicSSE(result, model))
		})
		return
	}

	result := s.parseUpstreamLogged(resp, rid, "[messages]")
	if e := errObj(result); e != nil {
		s.Log.Error(fmt.Sprintf("[%s] [messages] [502] 上游业务错误: %v", rid, e))
		writeJSON(w, http.StatusBadGateway, adapters.AnthropicErrorBody("api_error", fmt.Sprint(e)))
		return
	}
	writeJSON(w, http.StatusOK, adapters.ChatJSONToAnthropic(result, model))
}

// ── /v1/models（server.py 的模型路由）────────────────────────────────────────

func (s *Server) models(w http.ResponseWriter, r *http.Request) {
	// /v1/models/<model_id> 单模型详情
	if rest := strings.TrimPrefix(r.URL.Path, "/v1/models/"); rest != "" && rest != r.URL.Path {
		s.modelDetail(w, rest)
		return
	}
	s.C.RefreshIfNeeded()
	data := make([]any, 0)
	for _, id := range s.C.Supported() {
		data = append(data, s.C.ModelPayload(id))
	}
	writeJSON(w, http.StatusOK, map[string]any{"object": "list", "data": data})
}

func (s *Server) modelDetail(w http.ResponseWriter, modelID string) {
	s.C.RefreshIfNeeded()
	if !containsStr(s.C.Supported(), modelID) {
		writeJSON(w, http.StatusNotFound, map[string]any{
			"error": map[string]any{
				"message": fmt.Sprintf("model '%s' not found", modelID),
				"type":    "invalid_request_error",
				"code":    "model_not_found",
			}})
		return
	}
	writeJSON(w, http.StatusOK, s.C.ModelPayload(modelID))
}

// ── admin（对应 admin/panel.py）──────────────────────────────────────────────

func (s *Server) adminOverview(w http.ResponseWriter, r *http.Request) {
	s.C.RefreshIfNeeded()
	models := make([]map[string]any, 0)
	for _, mid := range s.C.Supported() {
		channel := "agent"
		if !s.C.IsAgent(mid) {
			channel = "benefit"
		}
		models = append(models, map[string]any{
			"id": mid, "channel": channel, "name": s.C.ModelPayload(mid)["name"],
		})
	}
	snap := s.C.Gate.Snapshot()
	writeJSON(w, http.StatusOK, map[string]any{
		"service":    "CodeArts2api",
		"credential": s.credInfo(),
		"models":     models,
		"benefit":    s.benefitInfo(),
		"schedule":   map[string]any{"auto_checkin": s.Cfg.BenefitAutoClaim},
		"session": map[string]any{
			"enabled":   s.Cfg.SessionGateOn,
			"limit":     snap["limit"],
			"in_use":    snap["in_use"],
			"heartbeat": snap["heartbeat"],
			"interval":  snap["interval"],
		},
	})
}

// credInfo 凭证卡：来源 / AK / 过期时间与剩余有效期（对应 panel.py _cred_info）。
func (s *Server) credInfo() map[string]any {
	creds := s.C.Creds.Get(false)
	notice := cred.TokenNotice(creds)
	if notice == "" {
		notice = "正常"
	}
	expiresAt, valid := "", false
	if creds != nil {
		expiresAt = creds.ExpiresAt
		valid = creds.Token != ""
	}
	remainText := "—"
	if expiresAt != "" && valid {
		if exp, err := time.Parse(time.RFC3339, strings.Replace(expiresAt, "Z", "+00:00", 1)); err == nil {
			remain := time.Until(exp)
			if remain > 0 {
				remainText = fmt.Sprintf("%d 小时 %d 分钟", int(remain.Hours()), int(remain.Minutes())%60)
			} else {
				remainText = "已过期"
				valid = false
			}
		}
	}
	source := "未找到"
	if creds != nil && creds.AuthFile != "" {
		source = "auths 文件"
	} else if valid {
		source = "CodeArts IDE 会话"
	}
	ak := ""
	if creds != nil {
		ak = creds.AK
	}
	return map[string]any{
		"source":      source,
		"ak":          ak,
		"expires_at":  expiresAt,
		"valid":       valid,
		"remain_text": remainText,
		"notice":      notice,
	}
}

// benefitInfo benefit 卡：每日额度 + 积分余额 + 活动列表 + 邀请链接。四项各自
// try：opengw（额度）和 snap-access（积分/活动/邀请）是两个 host，挂哪个都不该
// 让整份 overview 失败（对应 panel.py _benefit_info）。
func (s *Server) benefitInfo() map[string]any {
	info := map[string]any{}
	if status, err := s.Ben.ClaimStatus(); err == nil {
		// create_time 为毫秒时间戳，直接透传（前端 new Date 按毫秒解析）
		info["last_checkin"] = status["create_time"]
	} else {
		info["last_checkin"] = nil
	}
	if bal, err := s.Ben.Balance(); err == nil {
		for k, v := range bal {
			info[k] = v
		}
	}
	// 积分通道（snap-access 网关）与福利额度同属 benefit，一并返回
	if credits := s.Ben.CreditBalanceCached(); len(credits) > 0 {
		info["credit_total"] = credits["total"]
		info["credit_used"] = credits["used"]
		info["credit_remain"] = credits["remain"]
	}
	info["credit_activities"] = s.Ben.CreditActivitiesCached()
	info["invite"] = s.Ben.InviteLinkCached()
	return info
}

func (s *Server) adminCheckin(w http.ResponseWriter, r *http.Request) {
	if _, err := s.Ben.Claim(); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{
			"ok": false, "message": fmt.Sprintf("签到失败: %v", err)})
		return
	}
	// Python：balance 在 claim 的同一层 try 里，失败同样走「签到失败」500
	bal, err := s.Ben.Balance()
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{
			"ok": false, "message": fmt.Sprintf("签到失败: %v", err)})
		return
	}
	messages := []string{fmt.Sprintf("Token %s/%s",
		comma(numOr(bal["total_balance"])), comma(numOr(bal["total_quota"])))}
	if status, err := s.Ben.DoCreditClaim(); err == nil {
		s.Ben.InvalidateCreditCaches()
		if credits := s.Ben.CreditBalanceCached(); len(credits) > 0 {
			messages = append(messages, fmt.Sprintf("积分 %s/%s（%s）",
				comma(numOr(credits["remain"])), comma(numOr(credits["total"])), status))
		}
	} else {
		s.Log.Warn("积分领取失败（不影响 Token 签到）: " + err.Error())
	}
	writeJSON(w, http.StatusOK, map[string]any{
		"ok": true, "message": "签到成功：" + strings.Join(messages, "，")})
}

func (s *Server) adminReloadModels(w http.ResponseWriter, r *http.Request) {
	ok := s.C.RefreshModels()
	msg := fmt.Sprintf("已同步 %d 个模型", len(s.C.Supported()))
	if !ok {
		msg = "同步失败，使用缓存"
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": ok, "message": msg})
}

// ── 小工具 ───────────────────────────────────────────────────────────────────

// scanLines 按行扫上游 body（对应 iter_lines）。第二个返回值在流结束后给出
// 扫描错误（SSELines.Err 的 interrupted 判定用）。
func scanLines(body io.Reader) (iter.Seq[string], func() error) {
	var scanErr error
	seq := func(yield func(string) bool) {
		sc := bufio.NewScanner(body)
		sc.Buffer(make([]byte, 64<<10), 8<<20)
		for sc.Scan() {
			if !yield(sc.Text()) {
				return
			}
		}
		scanErr = sc.Err()
	}
	return seq, func() error { return scanErr }
}

func emitData(yield func(string) bool, v any) bool {
	return yield("data: " + string(marshalCompact(v)) + "\n\n")
}

func emitDone(yield func(string) bool) bool {
	return yield("data: [DONE]\n\n")
}

// normalizeChoicesFinish 聚合 JSON 的 finish_reason 归一（None/"other"→stop）。
func normalizeChoicesFinish(result map[string]any) {
	for _, ci := range asListAny(result["choices"]) {
		if choice := asMapAny(ci); choice != nil {
			if choice["finish_reason"] == nil || str(choice["finish_reason"]) == "other" {
				choice["finish_reason"] = "stop"
			}
		}
	}
}

func asListAny(v any) []any {
	l, _ := v.([]any)
	return l
}

func asMapAny(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

// orStrLocal Python `a or b` 语义：nil/空串回退。
func orStrLocal(v any, def string) string {
	if s, ok := v.(string); ok && s != "" {
		return s
	}
	if v != nil {
		return fmt.Sprint(v)
	}
	return def
}

func numOr(v any) float64 {
	switch t := v.(type) {
	case float64:
		return t
	case int:
		return float64(t)
	}
	return 0
}

func comma(n float64) string {
	s := fmt.Sprintf("%.0f", n)
	var out []byte
	for i, c := range []byte(s) {
		if i > 0 && (len(s)-i)%3 == 0 {
			out = append(out, ',')
		}
		out = append(out, c)
	}
	return string(out)
}
