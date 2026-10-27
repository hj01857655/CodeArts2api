// 模型双通道同步（对应 server.py 的模型段）：AgentCenter 老套餐（channel=agent）
// + opengw 免费额度网关（channel=benefit），models-cache.json 短缓存，
// 强制同步解决「上游刚投放 → 本地缓存追上」窗口期的 404。
package codearts

import (
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"codearts2api/internal/config"
)

const (
	modelCacheName        = "models-cache.json"
	modelCacheMaxAge      = 300 * time.Second
	forcedSyncMinInterval = 20 * time.Second
)

// LoadModelCache 加载本地模型缓存，返回是否成功。
func (c *Client) LoadModelCache() bool {
	raw, err := os.ReadFile(c.Cfg.StateFilePath(modelCacheName))
	if err != nil {
		return false
	}
	var cached struct {
		Models []map[string]any `json:"models"`
	}
	if json.Unmarshal(raw, &cached) != nil || len(cached.Models) == 0 {
		return false
	}
	models := make([]map[string]any, 0, len(cached.Models))
	for _, item := range cached.Models {
		if strOr(item["id"], "") != "" {
			models = append(models, item)
		}
	}
	if len(models) == 0 {
		return false
	}
	c.applyModels(models)
	return true
}

// saveModelCache 原子写缓存（tmp + replace，对齐 _save_model_cache）。
func (c *Client) saveModelCache(models []map[string]any) {
	payload, err := json.MarshalIndent(map[string]any{
		"updated_at": time.Now().Unix(),
		"models":     models,
	}, "", "  ")
	if err != nil {
		return
	}
	path := c.Cfg.StateFilePath(modelCacheName)
	_ = os.MkdirAll(filepath.Dir(path), 0o700)
	tmp := path + ".tmp"
	if os.WriteFile(tmp, payload, 0o600) == nil {
		_ = os.Rename(tmp, path)
	}
}

// applyModels 重绑定内存三件套（重绑而非原地改，调用方读快照）。
func (c *Client) applyModels(models []map[string]any) {
	supported := make([]string, 0, len(models))
	details := make(map[string]map[string]any, len(models))
	agent := map[string]bool{}
	for _, item := range models {
		id := strOr(item["id"], "")
		if id == "" {
			continue
		}
		supported = append(supported, id)
		details[id] = item
		// 旧缓存无 channel 字段时默认视为 agent 通道；不在缓存里的新模型按 benefit
		if strOr(item["channel"], "agent") == "agent" {
			agent[id] = true
		}
	}
	c.mu.Lock()
	c.supported = supported
	c.details = details
	c.agentModels = agent
	c.mu.Unlock()
}

// Supported 返回当前可用模型快照。
func (c *Client) Supported() []string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]string{}, c.supported...)
}

// IsAgent 通道判断：AgentCenter 老套餐用基础签名；其余（免费 benefit 模型）
// 必须带 maas_type/model-id/model-name 三个签名头。
func (c *Client) IsAgent(model string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.agentModels[model]
}

// benefitGatewayEnabled 问上游：这个账号的福利网关开没开。
// GET /v1/benefit-gateway-config → 裸对象 {"enabled": bool}（没有 {code,data} 信封）。
// 查询失败返回 true：只是「问不到」，不代表渠道关闭，别因一次网络抖动把福利模型永久关掉。
func (c *Client) benefitGatewayEnabled() bool {
	resp, err := c.SignedRequest("GET", config.BenefitGatewayConfigURL, nil,
		map[string]string{"agent-type": "PromptCenter", "x-language": "zh-cn"}, 15*time.Second)
	if err != nil {
		c.Log.Debug("查询福利网关总开关失败，按「未关闭」继续: " + err.Error())
		return true
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return true
	}
	var payload map[string]any
	if json.NewDecoder(resp.Body).Decode(&payload) != nil {
		return true
	}
	return truthy(payload["enabled"])
}

// isChannelDown 判断错误是不是「权益渠道未开通」而不是网络/签名问题。
// 上游把渠道问题也塞在 4000 码里，判据只能是 error_msg 里的渠道字样。
func isChannelDown(err error) bool {
	msg := err.Error()
	return strings.Contains(msg, "invalid channel") || strings.Contains(msg, "group not found")
}

// RefreshBenefitModels 从 opengw 免费额度网关动态获取免费模型列表。
// 渠道 2026-10 起对部分账号下线：先问总开关，关了就整体跳过（终态记忆，
// 之后不再每轮同步重试）。返回模型详情列表；失败返回空列表回退兜底。
func (c *Client) RefreshBenefitModels() []map[string]any {
	if c.benefitChannelDown {
		return nil
	}
	if !c.benefitGatewayEnabled() {
		c.setBenefitChannelDown()
		c.Log.Info("账号的福利网关未开通（benefit-gateway-config.enabled=false），" +
			"跳过免费模型同步；模型列表由 AgentCenter 提供")
		return nil
	}
	// 先领取限时福利（幂等）：未领取时福利模型调用必报 benefit not found。
	// 领取失败只记日志，不阻断模型列表同步。
	if c.Cfg.BenefitAutoClaim && c.Benefit != nil {
		if _, err := c.Benefit.Claim(); err != nil {
			if isChannelDown(err) {
				// 开关说开着、实际却回 invalid channel：以实际上游行为为准
				c.setBenefitChannelDown()
				c.Log.Info(fmt.Sprintf("福利网关开关为开，但上游回 invalid channel（%v），按未开通处理并跳过", err))
				return nil
			}
			c.Log.Warn(fmt.Sprintf("限时福利自动领取失败（不影响模型列表同步）: %v", err))
		}
	}
	resp, err := c.SignedRequest("GET", config.OpengwConfigURL, nil, nil, 30*time.Second)
	if err != nil {
		if isChannelDown(err) {
			c.setBenefitChannelDown()
			c.Log.Info("opengw 权益渠道未开通，免费模型列表回退到内置清单")
			return nil
		}
		c.Log.Warn("免费模型列表同步失败，将使用兜底列表: " + err.Error())
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		raw := readFew(resp.Body)
		if isChannelDown(errString(raw)) {
			c.setBenefitChannelDown()
			c.Log.Info("opengw 权益渠道未开通，免费模型列表回退到内置清单")
			return nil
		}
		c.Log.Warn(fmt.Sprintf("免费模型列表同步失败 HTTP %d，将使用兜底列表: %.200s", resp.StatusCode, raw))
		return nil
	}
	var body struct {
		Result struct {
			Models []map[string]any `json:"models"`
		} `json:"result"`
	}
	if json.NewDecoder(resp.Body).Decode(&body) != nil {
		return nil
	}
	var discovered []map[string]any
	for _, model := range body.Result.Models {
		modelID := strOr(model["model_id"], "")
		if modelID == "" || containsID(discovered, modelID) {
			continue
		}
		discovered = append(discovered, map[string]any{
			"id":              modelID,
			"object":          "model",
			"owned_by":        "codearts",
			"channel":         "benefit",
			"name":            orStr(model["model_name"], modelID),
			"description":     "免费额度模型（签到领取）",
			"context_window":  model["context_window"],
			"max_tokens":      model["max_tokens"],
			"supports_images": false,
		})
	}
	return discovered
}

func (c *Client) setBenefitChannelDown() {
	c.mu.Lock()
	c.benefitChannelDown = true
	c.mu.Unlock()
}

// RefreshModels 从 CodeArts AgentCenter 获取当前账号可用模型（老套餐通道）。
func (c *Client) RefreshModels() bool {
	agentHeaders := map[string]string{
		"agent-type": "AgentCenter",
		"x-language": "zh-cn",
		"accept":     "application/json",
	}
	resp, err := c.SignedRequest("GET", config.AgentListURL, nil, agentHeaders, 30*time.Second)
	if err != nil {
		c.Log.Warn("自动同步模型失败，将使用缓存或兜底列表: " + err.Error())
		return false
	}
	raw, _ := readAllBody(resp.Body)
	resp.Body.Close()
	var data struct {
		Agents []map[string]any `json:"agents"`
	}
	if json.Unmarshal(raw, &data) != nil || len(data.Agents) == 0 {
		return false
	}

	// 优先主 Agent；agent_order 升序（缺失排最后）；某个详情无模型再试其他主 Agent
	candidates := append([]map[string]any{}, data.Agents...)
	sort.SliceStable(candidates, func(i, j int) bool {
		a, b := candidates[i], candidates[j]
		pi, pj := truthy(a["is_primary_agent"]), truthy(b["is_primary_agent"])
		if pi != pj {
			return pi
		}
		oa, oka := orderOf(a["agent_order"])
		ob, okb := orderOf(b["agent_order"])
		if oka != okb {
			return oka // 无 order 的排后
		}
		return oa < ob
	})

	var discovered []map[string]any
	for _, agent := range candidates {
		agentID := strOr(agent["agent_id"], "")
		if agentID == "" {
			continue
		}
		detailURL := config.BaseURL + config.AgentDetailPath +
			"?agent_id=" + url.QueryEscape(agentID)
		dresp, err := c.SignedRequest("GET", detailURL, nil, agentHeaders, 30*time.Second)
		if err != nil || dresp.StatusCode != 200 {
			if dresp != nil {
				dresp.Body.Close()
			}
			continue
		}
		detailRaw, _ := readAllBody(dresp.Body)
		dresp.Body.Close()
		var detail struct {
			Gpts struct {
				Models []map[string]any `json:"models"`
			} `json:"gpts"`
		}
		if json.Unmarshal(detailRaw, &detail) != nil {
			continue
		}
		for _, model := range detail.Gpts.Models {
			modelID := orStr(model["model_id"], strOr(model["model_alias"], ""))
			if modelID == "" || containsID(discovered, modelID) {
				continue
			}
			params := orMap(model["model_parameters"], map[string]any{})
			discovered = append(discovered, map[string]any{
				"id":              modelID,
				"object":          "model",
				"owned_by":        "codearts",
				"channel":         "agent",
				"name":            orStr(model["model_alias"], orStr(model["model_name"], modelID)),
				"description":     strOr(model["model_desc"], ""),
				"context_window":  params["context_window"],
				"max_tokens":      params["max_tokens"],
				"supports_images": truthyOr(params["supports_images"], false),
				"enable_queue":    truthyOr(params["enable_queue"], false),
			})
		}
		if len(discovered) > 0 {
			// 主 Agent 已拿到完整模型列表，避免不必要请求
			break
		}
	}
	if len(discovered) == 0 {
		return false
	}

	// benefit 免费模型不在 AgentCenter 列表里，从 opengw 网关动态同步；
	// 失败回退 BENEFIT_MODELS 兜底，保证 /v1/models 可见
	benefitModels := c.RefreshBenefitModels()
	if len(benefitModels) == 0 {
		benefitModels = make([]map[string]any, 0, len(config.BenefitModels))
		for _, id := range config.BenefitModels {
			benefitModels = append(benefitModels, map[string]any{
				"id": id, "channel": "benefit", "object": "model",
				"owned_by": "codearts", "name": id,
			})
		}
	}
	known := map[string]bool{}
	for _, item := range discovered {
		known[strOr(item["id"], "")] = true
	}
	for _, item := range benefitModels {
		if !known[strOr(item["id"], "")] {
			discovered = append(discovered, item)
		}
	}
	c.applyModels(discovered)
	c.saveModelCache(discovered)
	ids := c.Supported()
	c.Log.Info("已自动同步模型: " + strings.Join(ids, ", "))
	return true
}

func orderOf(v any) (float64, bool) {
	switch t := v.(type) {
	case float64:
		return t, true
	case int:
		return float64(t), true
	}
	return 999999, false
}

// RefreshIfNeeded 模型列表短缓存：过期时完全不缺模型才后台刷新，
// 当前请求沿用旧列表（上游变慢不该拖住对话）。
func (c *Client) RefreshIfNeeded() {
	if len(c.details) == 0 {
		c.LoadModelCache() // 进程刚启动先把缓存详情装载，不能只靠兜底模型名
	}
	age, err := fileAge(c.Cfg.StateFilePath(modelCacheName))
	if err == nil && age <= modelCacheMaxAge {
		return
	}
	if len(c.Supported()) == 0 {
		if !c.RefreshModels() {
			c.LoadModelCache()
		}
		return
	}
	if c.refreshInProgress.CompareAndSwap(false, true) {
		go func() {
			defer c.refreshInProgress.Store(false)
			c.RefreshModels()
		}()
	}
}

// ForceSyncFor 请求的模型不在列表里时同步再同步一次，返回是否因此命中。
// 背景：RefreshIfNeeded 过期时只起后台刷新，「上游刚放出模型」到「缓存追上」
// 之间有窗口期，期间每次请求都被本地误判 404。带 20s 节流：一个模型名查不到
// 可能是客户端配置写错并在重试，没有节流就变成每次坏请求都打一遍上游。
func (c *Client) ForceSyncFor(model string) bool {
	now := time.Now().Unix()
	if now-c.lastForcedSync.Load() < int64(forcedSyncMinInterval.Seconds()) {
		return c.inSupported(model)
	}
	c.lastForcedSync.Store(now)
	c.RefreshModels()
	return c.inSupported(model)
}

func (c *Client) inSupported(model string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, m := range c.supported {
		if m == model {
			return true
		}
	}
	return false
}

// CappedMaxTokens 把云端下发的输出上限收敛到上游实际接受的值。
func CappedMaxTokens(v any, cap int) any {
	switch t := v.(type) {
	case float64:
		if t > float64(cap) {
			return cap
		}
		return t
	case int:
		if t > cap {
			return cap
		}
		return t
	}
	return v
}

// ClampMaxTokens 把客户端要的输出上限钳到上游接受范围（原地改 req）。
// 超出时 InferHub 不返回 4xx 而是在已建立的流里塞错误帧，Agent Runtime 会当
// 空响应无限重试；必须在转发前修正。钳制必须在签名前完成——签名覆盖 body 哈希。
func (c *Client) ClampMaxTokens(req map[string]any, rid string) {
	for _, field := range []string{"max_tokens", "max_completion_tokens"} {
		v, ok := asFloat(req[field])
		if !ok || v <= float64(c.Cfg.MaxTokensCap) {
			continue
		}
		req[field] = float64(c.Cfg.MaxTokensCap)
		c.Log.Warn(fmt.Sprintf("[%s] %s %d→%d：超过上游上限，超出部分会被 InferHub 判为非法参数",
			rid, field, int(v), c.Cfg.MaxTokensCap))
	}
}

// ModelPayload 生成 OpenAI 模型对象并附带常见能力元数据。
// 对外上报的输出上限必须钳到上游真正接受的值，否则客户端按云端下发的
// 上下文口径值（131072+）发请求直接被 InferHub 判非法。
func (c *Client) ModelPayload(modelID string) map[string]any {
	c.mu.Lock()
	detail := c.details[modelID]
	c.mu.Unlock()
	if detail == nil {
		detail = map[string]any{}
	}
	contextWindow := detail["context_window"]
	maxTokens := CappedMaxTokens(detail["max_tokens"], c.Cfg.MaxTokensCap)
	supportsImages := truthyOr(detail["supports_images"], false)
	payload := map[string]any{
		"id":                        modelID,
		"object":                    "model",
		"created":                   0,
		"owned_by":                  "codearts",
		"name":                      orStr(detail["name"], modelID),
		"description":               strOr(detail["description"], ""),
		"context_window":            contextWindow,
		"max_tokens":                maxTokens,
		"supports_images":           supportsImages,
		"supports_function_calling": true,
		"supports_tool_calling":     true,
		"supports_reasoning":        true,
		"output_modalities":         []string{"text"},
		"supported_parameters": []string{
			"temperature", "top_p", "max_tokens", "stream",
			"tools", "tool_choice", "response_format",
		},
	}
	if contextWindow != nil {
		payload["context_length"] = contextWindow
	} else {
		delete(payload, "context_window")
	}
	if maxTokens != nil {
		payload["max_output_tokens"] = maxTokens
	} else {
		delete(payload, "max_tokens")
	}
	payload["vision"] = supportsImages
	payload["supports_vision"] = supportsImages
	if supportsImages {
		payload["input_modalities"] = []string{"text", "image"}
	} else {
		payload["input_modalities"] = []string{"text"}
	}
	return payload
}

// readFew 读少量错误体文本（日志用）。
func readFew(r io.Reader) string {
	buf := make([]byte, 4096)
	n, _ := io.ReadFull(r, buf)
	if n < 0 {
		n = 0
	}
	return string(buf[:n])
}

func containsID(models []map[string]any, id string) bool {
	for _, m := range models {
		if strOr(m["id"], "") == id {
			return true
		}
	}
	return false
}

func fileAge(path string) (time.Duration, error) {
	info, err := os.Stat(path)
	if err != nil {
		return 0, err
	}
	return time.Since(info.ModTime()), nil
}
