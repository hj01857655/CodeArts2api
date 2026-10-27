// Package cred 对应 Python lanes/credential_provider.py：加载并自动续期
// CodeArts 网关临时凭证（AK/SK/securityToken，数小时过期）。
//
// 两条装载路径：auths/*.json（oauth 登录生成，优先）→ CodeArts IDE 本地会话
// （%APPDATA%\codearts-agent / %APPDATA%\codearts，state.vscdb 内 os_crypt
// AES-GCM 加密，DPAPI 保护主密钥）。30s 缓存；临期（<600s）自动用
// refresh_token + PKCE verifier + DPoP ES256 向 STS 端点续期。
//
// 与 Python 版的一处有意差异：IDE 会话刷新后不回写 state.vscdb（Go 侧
// sqlitedb 是只读解析器）。刷新结果只活在进程内存里；IDE 自己也会刷新，
// 回写只为让 IDE 共享新 token，跳过不影响本代理取数。
package cred

import (
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

// ClientID 是 STS 端点的 OAuth client（与 IDE 扩展一致）。
const ClientID = "codearts-agent"

// refreshBefore 临期续期阈值（Python _REFRESH_BEFORE）。
const refreshBefore = 600 * time.Second

// Cred 一把可用凭证。AccountID 是跨 AK 轮换稳定的账号身份（并发闸门的 key
// 必须用它，不能用 AK：AK 每次续期都换，会让 3 路上限被悄悄重置）。
type Cred struct {
	AK, SK, Token string
	ExpiresAt     string
	AccountID     string

	// auth 文件路径（"" 表示来自 IDE 会话）
	AuthFile string

	// 续期三件套（缺任一无法自助续期）
	RefreshToken string
	CodeVerifier string
	DPoPJWK      map[string]string
	ClientID     string
}

// Provider 凭证装载器。StateDir 下找 auths/；IDE 目录固定 %APPDATA% 两个。
type Provider struct {
	StateDir string
	HTTP     *http.Client
	// STSURL 可覆盖（测试指假上游）；空则用官方端点
	STSURL string

	mu      sync.Mutex
	cache   *Cred
	cacheAt time.Time
}

func (p *Provider) stsURL() string {
	if p.STSURL != "" {
		return p.STSURL
	}
	return "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
}

// Get 返回当前凭证（30s 缓存）；找不到返回 nil。force=true 跳过缓存并重扫
// （上游判凭证失效时调用方强制重读，内部会触发续期）。
func (p *Provider) Get(force bool) *Cred {
	p.mu.Lock()
	defer p.mu.Unlock()
	if !force && p.cache != nil && time.Since(p.cacheAt) < 30*time.Second {
		return p.cache
	}
	now := time.Now()
	cred := p.loadAuthFiles()
	if cred == nil {
		cred = p.scanIDE()
	}
	p.cacheAt = now
	p.cache = cred
	return cred
}

// Empty 空凭证（上游会拒绝并给出明确错误）。
func Empty() *Cred { return &Cred{} }

// authsDir 对齐 Python：仓库根/auths（oauth.py 的 AUTHS_DIR 是 lanes/../auths，
// 即与 server.py、models-cache.json 同级）。StateDir 部署在原仓库目录时正好接上。
func (p *Provider) authsDir() string { return filepath.Join(p.StateDir, "auths") }

// loadAuthFiles 从 auths/*.json 加载凭证（按文件名排序取第一个可用），
// 临期时刷新并回写文件。
func (p *Provider) loadAuthFiles() *Cred {
	entries, err := os.ReadDir(p.authsDir())
	if err != nil {
		return nil
	}
	names := make([]string, 0, len(entries))
	for _, e := range entries {
		name := e.Name()
		if !e.IsDir() && strings.HasPrefix(name, "codearts-") && strings.HasSuffix(name, ".json") {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	for _, name := range names {
		path := filepath.Join(p.authsDir(), name)
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var data map[string]any
		if json.Unmarshal(raw, &data) != nil {
			continue
		}
		ak := strings.TrimSpace(str(data["access_key_id"]))
		sk := strings.TrimSpace(str(data["secret_access_key"]))
		token := strings.TrimSpace(str(data["cloud_dragon_token"]))
		if ak == "" || sk == "" || token == "" {
			continue
		}
		cred := &Cred{
			AK:           ak,
			SK:           sk,
			Token:        token,
			ExpiresAt:    str(data["expiration"]),
			RefreshToken: str(data["refresh_token"]),
			CodeVerifier: str(data["code_verifier"]),
			ClientID:     orDefault(str(data["client_id"]), ClientID),
			AuthFile:     path,
			// 闸门身份：文件名去扩展名，与 AK 轮换无关
			AccountID: strings.TrimSuffix(name, ".json"),
		}
		if jwk, ok := data["dpop_private_key"].(map[string]any); ok {
			cred.DPoPJWK = strMap(jwk)
		}
		if NeedsRefresh(cred) {
			if refreshed := p.refreshAuthFile(cred); refreshed != nil {
				cred = refreshed
			}
		}
		return cred
	}
	return nil
}

// refreshAuthFile 刷新 auths/*.json 凭证并回写文件（对齐 _refresh_auth_file）。
func (p *Provider) refreshAuthFile(c *Cred) *Cred {
	if c.RefreshToken == "" || c.CodeVerifier == "" || c.DPoPJWK["d"] == "" {
		missing := missingNames(c.RefreshToken, c.CodeVerifier, c.DPoPJWK["d"])
		fmt.Printf("[credential_provider] auth 文件跳过刷新：缺 %s\n", strings.Join(missing, "、"))
		return nil
	}
	creds, newRefresh, ok := p.exchange(c.ClientID, c.CodeVerifier, c.RefreshToken, c.DPoPJWK)
	if !ok {
		return nil
	}
	original, err := os.ReadFile(c.AuthFile)
	if err != nil {
		return nil
	}
	var data map[string]any
	if json.Unmarshal(original, &data) != nil {
		return nil
	}
	data["access_key_id"] = creds["access_key_id"]
	data["secret_access_key"] = creds["secret_access_key"]
	data["cloud_dragon_token"] = creds["security_token"]
	data["expiration"] = creds["expiration"]
	data["refresh_token"] = newRefresh
	data["updated_at"] = time.Now().Unix()
	out, _ := json.MarshalIndent(data, "", "  ")
	if os.WriteFile(c.AuthFile, out, 0o600) != nil {
		return nil
	}
	fmt.Println("[credential_provider] auth 文件凭证已自动刷新")
	c.AK = creds["access_key_id"]
	c.SK = creds["secret_access_key"]
	c.Token = creds["security_token"]
	c.ExpiresAt = creds["expiration"]
	c.RefreshToken = newRefresh
	return c
}

// NeedsRefresh 对齐 _needs_refresh：有过期时间且有 refresh_token、
// 距过期不足 600s 才需要续期。解析失败视为不需要。
func NeedsRefresh(c *Cred) bool {
	if c.ExpiresAt == "" || c.RefreshToken == "" {
		return false
	}
	exp, err := parseTime(c.ExpiresAt)
	if err != nil {
		return false
	}
	return time.Until(exp) <= refreshBefore
}

// TokenNotice 过期/缺失的友好提示（对齐 token_notice）。
func TokenNotice(c *Cred) string {
	if c == nil {
		return "（本地未找到 CodeArts 会话凭证，请在 CodeArts IDE/网页中登录后再试）"
	}
	if c.ExpiresAt == "" || c.Token == "" {
		return ""
	}
	exp, err := parseTime(c.ExpiresAt)
	if err != nil {
		return ""
	}
	remain := time.Until(exp)
	if remain <= 0 {
		return "（会话凭证已过期且无法自动刷新，请在 CodeArts IDE/网页中重新登录）"
	}
	if remain < 300*time.Second {
		return fmt.Sprintf("（会话凭证即将过期，剩余 %d 分钟）", int(remain.Minutes()))
	}
	return ""
}

// ── 小工具 ───────────────────────────────────────────────────────────────────

func str(v any) string {
	s, _ := v.(string)
	return s
}

func strMap(m map[string]any) map[string]string {
	out := make(map[string]string, len(m))
	for k, v := range m {
		out[k] = str(v)
	}
	return out
}

func orDefault(v, def string) string {
	if v == "" {
		return def
	}
	return v
}

// parseTime 兼容 "…Z" 与带偏移的 ISO8601。
func parseTime(s string) (time.Time, error) {
	return time.Parse(time.RFC3339, strings.Replace(s, "Z", "+00:00", 1))
}

// missingNames 对齐 Python 的缺项提示顺序。
func missingNames(vals ...string) []string {
	names := []string{"refresh_token", "code_verifier", "dpop_private_key.d"}
	var missing []string
	for i, v := range vals {
		if v == "" && i < len(names) {
			missing = append(missing, names[i])
		}
	}
	return missing
}
