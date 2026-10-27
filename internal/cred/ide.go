// STS 令牌端点刷新（Python _try_refresh / _refresh_auth_file 共用的 exchange
// 逻辑）与 IDE 本地会话扫描。
package cred

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"codearts2api/internal/sqlitedb"
)

// exchange 向 STS 端点换新凭证（form 表单 + DPoP ES256 头）。
// 返回 (credentials 字段, 新 refresh_token, 是否成功)。
func (p *Provider) exchange(clientID, verifier, refreshToken string, jwk map[string]string) (map[string]string, string, bool) {
	httpc := p.HTTP
	if httpc == nil {
		httpc = &http.Client{Timeout: 30 * time.Second}
	}
	stsURL := p.stsURL()
	proof, err := DPoPProof(jwk, stsURL)
	if err != nil {
		fmt.Printf("[credential_provider] DPoP proof 生成失败: %v\n", err)
		return nil, "", false
	}
	form := url.Values{
		"client_id":     {clientID},
		"code_verifier": {verifier},
		"grant_type":    {"refresh_token"},
		"refresh_token": {refreshToken},
	}
	req, err := http.NewRequest("POST", stsURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, "", false
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("DPoP", proof)
	req.Header.Set("User-Agent", "CodeArts2api/1.0")
	resp, err := httpc.Do(req)
	if err != nil {
		fmt.Printf("[credential_provider] 令牌刷新异常: %v\n", err)
		return nil, "", false
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != 200 {
		fmt.Printf("[credential_provider] 令牌刷新失败 HTTP %d: %.200s\n", resp.StatusCode, body)
		return nil, "", false
	}
	var parsed map[string]any
	if json.Unmarshal(body, &parsed) != nil {
		return nil, "", false
	}
	creds := asMap(parsed["credentials"])
	if creds == nil {
		creds = asMap(parsed["credential"])
	}
	newRefresh := str(parsed["refresh_token"])
	ak := orDefault(str(creds["access_key_id"]), str(creds["access"]))
	sk := orDefault(str(creds["secret_access_key"]), str(creds["secret"]))
	token := orDefault(str(creds["security_token"]), str(creds["securityToken"]))
	if ak == "" || sk == "" || token == "" || newRefresh == "" {
		return nil, "", false
	}
	return map[string]string{
		"access_key_id":     ak,
		"secret_access_key": sk,
		"security_token":    token,
		"expiration":        orDefault(str(creds["expiration"]), str(creds["expires_at"])),
	}, newRefresh, true
}

// ideDirs 对齐 _ide_dirs 的 reversed 顺序（codearts-agent 优先）。
func ideDirs() []string {
	appdata := os.Getenv("APPDATA")
	return []string{
		filepath.Join(appdata, "codearts"),
		filepath.Join(appdata, "codearts-agent"),
	}
}

// secretKey state.vscdb 里加密会话的键（VSCode secret storage 形状）。
const secretKey = `secret://{"extensionId":"huaweicloud.authentication","key":"HuaweiCloudSession"}`

// plaintextStateKey 明文 userInfoKey 的键（旧凭证回退）。
const plaintextStateKey = "huaweicloud.codearts-snap"

// scanIDE 按优先级取一个 IDE 目录下的凭证（临期自动刷新，刷新只更新内存——
// Go 侧不回写 state.vscdb，见包注释）。
func (p *Provider) scanIDE() *Cred {
	for _, dir := range ideDirs() {
		if dir == "" {
			continue
		}
		if c := p.scanOne(dir); c != nil {
			return c
		}
	}
	return nil
}

func (p *Provider) scanOne(dir string) *Cred {
	if session := decryptSession(dir); session != nil {
		if needsSessionRefresh(session) {
			if refreshed := p.tryRefreshSession(session); refreshed != nil {
				session = refreshed
			}
		}
		if c := sessionToCred(session); c != nil {
			return c
		}
	}
	// 明文 userInfoKey（旧凭证，回退）
	if origin := asMap(asMap(readState(dir))["userInfoKey"])["originData"]; origin != nil {
		if m := asMap(origin); m != nil {
			if c := sessionToCred(m); c != nil {
				return c
			}
		}
	}
	return nil
}

// decryptSession 解密 HuaweiCloudSession（对应 _decrypt_session）。
func decryptSession(dir string) map[string]any {
	db := filepath.Join(dir, "User", "globalStorage", "state.vscdb")
	if _, err := os.Stat(db); err != nil {
		return nil
	}
	key, err := oscryptKey(dir)
	if err != nil || key == nil {
		return nil
	}
	raw, ok, err := sqlitedb.Lookup(db, "ItemTable", secretKey)
	if err != nil || !ok {
		return nil
	}
	buf, err := rawToBytes(raw)
	if err != nil || !hasPrefix(buf, "v10") {
		return nil
	}
	if len(buf) < 15+16 {
		return nil
	}
	plain, err := aesGCMOpen(key, buf[3:15], buf[15:])
	if err != nil {
		return nil
	}
	var session map[string]any
	if json.Unmarshal(plain, &session) != nil {
		return nil
	}
	return session
}

// needsSessionRefresh 对会话 dict 判断临期（_needs_refresh 的 map 版）。
func needsSessionRefresh(session map[string]any) bool {
	exp := str(session["expires_at"])
	if exp == "" || str(session["refresh_token"]) == "" {
		return false
	}
	t, err := parseTime(exp)
	if err != nil {
		return false
	}
	return time.Until(t) <= refreshBefore
}

// tryRefreshSession 刷新 IDE 会话凭证。Go 版不回写 state.vscdb（sqlitedb 只读），
// 刷新结果只活在本次返回的 session 里。
func (p *Provider) tryRefreshSession(session map[string]any) map[string]any {
	loginCtx := asMap(session["loginContext"])
	pkce := asMap(loginCtx["pkcePair"])
	dpop := asMap(loginCtx["dpopKeyPair"])
	verifier := orDefault(str(pkce["codeVerifier"]), str(pkce["code_verifier"]))
	jwk := strMap(orMap(dpop["privateKeyJwk"], dpop["private_key_jwk"]))
	refreshToken := str(session["refresh_token"])
	if refreshToken == "" || verifier == "" || jwk["d"] == "" {
		// 缺任一项就得不出 DPoP proof；不打原因会像“没反应”，只看到上游持续 401
		var missing []string
		if refreshToken == "" {
			missing = append(missing, "refresh_token")
		}
		if verifier == "" {
			missing = append(missing, "loginContext.pkcePair.codeVerifier")
		}
		if jwk["d"] == "" {
			missing = append(missing, "loginContext.dpopKeyPair.privateKeyJwk")
		}
		fmt.Printf("[credential_provider] IDE 会话凭证跳过刷新：缺 %s\n", strings.Join(missing, "、"))
		return nil
	}
	creds, newRefresh, ok := p.exchange(ClientID, verifier, refreshToken, jwk)
	if !ok {
		return nil
	}
	updated := cloneMap(session)
	updated["accessKey"] = creds["access_key_id"]
	updated["secretKey"] = creds["secret_access_key"]
	updated["securitytoken"] = creds["security_token"]
	updated["expires_at"] = creds["expiration"]
	updated["refresh_token"] = newRefresh
	fmt.Println("[credential_provider] 会话凭证已自动刷新")
	return updated
}

// sessionToCred 对齐 _session_to_cred：取 AK/SK/token，身份串用 account.id
// （登录账号主键，续期不变）→ accountId → domainId → userId → userName。
func sessionToCred(session map[string]any) *Cred {
	ak := strings.TrimSpace(str(session["accessKey"]))
	sk := strings.TrimSpace(str(session["secretKey"]))
	token := strings.TrimSpace(orDefault(str(session["securitytoken"]), str(session["securityToken"])))
	if ak == "" || sk == "" || token == "" {
		return nil
	}
	account := asMap(session["account"])
	accountID := orDefault(str(account["id"]),
		orDefault(str(session["accountId"]),
			orDefault(str(session["domainId"]),
				orDefault(str(session["userId"]), str(session["userName"])))))
	return &Cred{
		AK:           ak,
		SK:           sk,
		Token:        token,
		ExpiresAt:    str(session["expires_at"]),
		AccountID:    accountID,
		RefreshToken: str(session["refresh_token"]),
	}
}

// readState 读取明文 huaweicloud.codearts-snap（旧凭证回退，_read_state）。
func readState(dir string) map[string]any {
	db := filepath.Join(dir, "User", "globalStorage", "state.vscdb")
	if _, err := os.Stat(db); err != nil {
		return nil
	}
	raw, ok, err := sqlitedb.Lookup(db, "ItemTable", plaintextStateKey)
	if err != nil || !ok {
		return nil
	}
	var state map[string]any
	if json.Unmarshal(raw, &state) != nil {
		return nil
	}
	return state
}

// rawToBytes 对齐 _raw_to_bytes：sqlite 里的值可能是 {"type":"Buffer","data":[…]}
// JSON（VSCode 存二进制的常见形态），否则按 latin-1 装箱的字符串还原原始字节。
// Go 读出的 TEXT 是 UTF-8 字节；Python 语义是「字符串码点逐个取低 8 位」，
// 即 utf8 解码后每 rune 取一个字节，码点 >0xFF 时失败（Python 会抛
// UnicodeEncodeError 被外层吞掉，这里同样报错放弃）。
func rawToBytes(raw []byte) ([]byte, error) {
	trimmed := trimSpaceBytes(raw)
	if hasPrefix(trimmed, "{") {
		var obj struct {
			Type string `json:"type"`
			Data []int  `json:"data"`
		}
		if json.Unmarshal(trimmed, &obj) == nil && obj.Type == "Buffer" {
			out := make([]byte, len(obj.Data))
			for i, n := range obj.Data {
				out[i] = byte(n)
			}
			return out, nil
		}
	}
	if !utf8Valid(trimmed) {
		return nil, fmt.Errorf("secret value is neither Buffer JSON nor valid UTF-8")
	}
	runes := []rune(string(trimmed))
	out := make([]byte, len(runes))
	for i, r := range runes {
		if r > 0xFF {
			return nil, fmt.Errorf("codepoint U+%04X out of latin-1 range", r)
		}
		out[i] = byte(r)
	}
	return out, nil
}

func asMap(v any) map[string]any {
	m, _ := v.(map[string]any)
	return m
}

func orMap(a, b any) map[string]any {
	if m := asMap(a); m != nil {
		return m
	}
	return asMap(b)
}

func cloneMap(m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	return out
}
