// Package config 装配 CodeArts2api 的运行配置：.env 加载 + 上游常量 + 状态目录。
//
// 环境变量名与默认值逐项对应 Python 版 server.py / lanes/session.py（迁移要求
// 两版可共用同一个 .env）：Python 读的是 CODEARTS_PROXY_PORT，而仓库里那份
// .env 写的是 PORT=8787 —— 也就是说 Python 版实际用的是默认值 8787，PORT 那行
// 根本没生效。这里保持同样的取值顺序（CODEARTS_PROXY_PORT 优先，不设则 8787），
// 不去"顺手修正"成读 PORT，否则两版行为就不一致了。
package config

import (
	"bufio"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// 上游端点常量（流量分析确认，与 Python server.py 一致）
const (
	BaseURL         = "https://snap-access.cn-north-4.myhuaweicloud.com"
	Host            = "snap-access.cn-north-4.myhuaweicloud.com"
	Target          = BaseURL + "/api/v2/chat/completions"
	AgentListURL    = BaseURL + "/v1/agent-center/agents/useragents?offset=0&limit=100"
	AgentDetailPath = "/v1/agent-center/agents/detail"
	// 福利网关总开关挂在 snap-access 域，不在 opengw 域（opengw 上该路径
	// 404 APIG.0101）。返回裸对象 {"enabled": bool}，没有 {code,data} 信封。
	BenefitGatewayConfigURL = BaseURL + "/v1/benefit-gateway-config"

	OpengwBase      = "https://opengw.developer.huaweicloud.com"
	OpengwConfigURL = OpengwBase + "/api/v1/gateway/config"

	// StsTokenURL 与 ClientID：refresh_token 续期端点（credential/oauth 共用）。
	StsTokenURL = "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
	ClientID    = "codearts-agent"

	UserAgent = "CodeArts2api/1.0"

	// UpstreamBaseOverride 环境变量：把 snap-access / opengw 两个上游域整体
	// 换成调试地址（冒烟指黑洞，绝不打真实上游）。生产不设置。
	UpstreamBaseOverride = "CODEARTS_UPSTREAM_BASE"
)

// 上游业务错误码语义（Python server.py 同名常量）。
var (
	// UpstreamRetryCodes 并发限流：可退避重试。
	UpstreamRetryCodes = map[string]bool{"TM.00001041": true}
	// UpstreamAuthCodes 临时凭证过期（APIG.0602）与 AK 校验失败（APIG.0301）：
	// 重试同一把凭证结果永远一样，必须先换凭证。
	UpstreamAuthCodes = map[string]bool{"APIG.0602": true, "APIG.0301": true}
	// UpstreamCreditCodes 积分耗尽：不可重试，直接透传语义。
	UpstreamCreditCodes = map[string]bool{"TM.00001050": true}
)

const (
	UpstreamRetryMax     = 3
	UpstreamRetryBackoff = 2.0
)

// FallbackAgentModels 老套餐兜底列表（仅自动同步失败时使用，走 AgentCenter 通道）。
var FallbackAgentModels = []string{"openpangu-2.0-pro", "openpangu-2.0-flash", "GLM-5.2"}

// BenefitModels 免费模型兜底列表（IDE 流量分析确认）：走 maas_type=benefit 通道，
// 签名时必须额外携带 maas_type / model-id / model-name 三个头，否则上游报
// "model is not registered"；老套餐模型带上这三个头反报 "unsupported model"。
var BenefitModels = []string{"glm-5.3-flash", "deepseek-v4-pro-0813", "deepseek-v4-flash-0731"}

// Config 全部运行期配置。
type Config struct {
	ListenHost string
	Port       int
	APIKey     string

	StateDir     string // models-cache.json / request.log / dumps/ 所在
	DumpOn       bool
	DumpDir      string
	DumpMaxFiles int

	UpstreamBase string // "" = 官方端点；冒烟/调试指向假上游

	BenefitAutoClaim bool
	SessionGateOn    bool
	MaxTokensCap     int
	ConnRetryMax     int
	ConnRetryBackoff float64

	UpstreamRetryMax     int
	UpstreamRetryBackoff float64
}

// StateFilePath 返回状态目录下的文件路径。
func (c *Config) StateFilePath(name string) string { return filepath.Join(c.StateDir, name) }

// Load 读取配置。优先级：环境变量 > .env > 默认值。
// .env 从状态目录和工作目录两处尝试（与 python-dotenv 的 load_dotenv 语义近似：
// 不展开变量、不覆盖已存在的 env）。
func Load() *Config {
	c := &Config{Port: 8787}

	// 状态目录：env 显式指定 > exe 所在目录。部署时把 exe 放进原 CodeArts2api
	// 目录（或设 CODEARTS_STATE_DIR 指向它），即可直接接管 Python 版留下的
	// models-cache.json 与 dumps/，两版交替读写同一批文件。
	c.StateDir = os.Getenv("CODEARTS_STATE_DIR")
	if c.StateDir == "" {
		if exe, err := os.Executable(); err == nil {
			c.StateDir = filepath.Dir(exe)
		} else {
			wd, _ := os.Getwd()
			c.StateDir = wd
		}
	}
	for _, dir := range []string{c.StateDir, mustWd()} {
		loadDotEnv(filepath.Join(dir, ".env"))
	}

	c.ListenHost = envOr("CODEARTS_PROXY_HOST", "127.0.0.1")
	if p, err := strconv.Atoi(strings.TrimSpace(os.Getenv("CODEARTS_PROXY_PORT"))); err == nil && p > 0 {
		c.Port = p
	}
	c.APIKey = strings.TrimSpace(os.Getenv("API_KEY"))
	c.UpstreamBase = strings.TrimSpace(os.Getenv(UpstreamBaseOverride))

	c.BenefitAutoClaim = envTruthy("CODEARTS_BENEFIT_AUTO_CLAIM", "1")
	c.SessionGateOn = envTruthy("CODEARTS_SESSION_GATE", "1")
	c.MaxTokensCap = envInt("CODEARTS_MAX_TOKENS_CAP", 65536)
	c.ConnRetryMax = envInt("CODEARTS_CONN_RETRY_MAX", 2)
	c.ConnRetryBackoff = envFloat("CODEARTS_CONN_RETRY_BACKOFF", 0.5)
	c.UpstreamRetryMax = UpstreamRetryMax
	c.UpstreamRetryBackoff = UpstreamRetryBackoff

	c.DumpOn = strings.TrimSpace(os.Getenv("DUMP")) != "0"
	c.DumpDir = strings.TrimSpace(os.Getenv("DUMP_DIR"))
	if c.DumpDir == "" {
		c.DumpDir = filepath.Join(c.StateDir, "dumps")
	}
	c.DumpMaxFiles = envInt("DUMP_MAX_FILES", 200)
	return c
}

// envTruthy 与 Python 的 `not in ("0","false","False","no","")` 判定一致：
// 只认这五个值为假，且比较前先 strip。
func envTruthy(key, def string) bool {
	v := strings.TrimSpace(envOr(key, def))
	switch v {
	case "0", "false", "False", "no", "":
		return false
	}
	return true
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func envInt(key string, def int) int {
	if n, err := strconv.Atoi(strings.TrimSpace(os.Getenv(key))); err == nil {
		return n
	}
	return def
}

func envFloat(key string, def float64) float64 {
	if f, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv(key)), 64); err == nil {
		return f
	}
	return def
}

func mustWd() string {
	wd, err := os.Getwd()
	if err != nil {
		return "."
	}
	return wd
}

// loadDotEnv 极简 .env 解析：KEY=VALUE 行，# 注释，不展开变量，不覆盖已有 env。
func loadDotEnv(path string) {
	f, err := os.Open(path)
	if err != nil {
		return
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.Trim(strings.TrimSpace(v), `"'`)
		if k != "" && os.Getenv(k) == "" {
			_ = os.Setenv(k, v)
		}
	}
}
