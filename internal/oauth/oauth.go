// Package oauth CodeArts OAuth2 PKCE 浏览器登录（对应 Python lanes/oauth.py）：
// 脱离 IDE 独立获取华为云 CodeArts 临时凭证（AK/SK/securityToken + refresh_token），
// 落盘 auths/codearts-{user_id}.json 后由 cred 包自动加载和续期。
package oauth

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"codearts2api/internal/cred"
)

const (
	clientID      = "codearts-agent"
	portalHost    = "https://codearts.huaweicloud.com/portal"
	redirectPath  = "/oauth/callback"
	pluginName    = "snap_AIIDE"
	pluginVersion = "5.2.0"
)

func b64u(data []byte) string {
	return base64.RawURLEncoding.EncodeToString(data)
}

// GeneratePKCE 生成 code_verifier + code_challenge (S256)。
func GeneratePKCE() (verifier, challenge string) {
	raw := make([]byte, 64)
	_, _ = rand.Read(raw)
	verifier = b64u(raw)
	sum := sha256.Sum256([]byte(verifier))
	return verifier, b64u(sum[:])
}

// GenerateDPoPKey 生成 DPoP ES256/P-256 密钥对，返回 JWK（含私钥 d）。
func GenerateDPoPKey() (map[string]string, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	return map[string]string{
		"kty": "EC",
		"crv": "P-256",
		"x":   b64u(key.PublicKey.X.FillBytes(make([]byte, 32))),
		"y":   b64u(key.PublicKey.Y.FillBytes(make([]byte, 32))),
		"d":   b64u(key.D.FillBytes(make([]byte, 32))),
	}, nil
}

// BuildAuthorizeURL 构造华为云 OAuth 授权链接（对齐 huaweicloud.authentication 扩展）。
func BuildAuthorizeURL(ticketID, challenge string, port int) string {
	q := url.Values{}
	q.Set("theme", "2")
	q.Set("locale", "zh-cn")
	q.Set("uri_scheme", clientID)
	q.Set("client_id", clientID)
	q.Set("port", strconv.Itoa(port))
	q.Set("code_challenge", challenge)
	q.Set("code_challenge_method", "SHA-256")
	q.Set("ticket_id", ticketID)
	q.Set("plugin-name", pluginName)
	q.Set("plugin-version", pluginVersion)
	return portalHost + "/authorize?" + q.Encode()
}

// ExchangeCode 用授权码换 STS 临时凭证（OAuth2 authorization_code grant）。
// 返回键与 Python exchange_code 相同：user_id/user_name/domain_id/access_key_id/
// secret_access_key/security_token/expiration/refresh_token。
func ExchangeCode(client *http.Client, code, verifier string, port int, dpopJWK map[string]string) (map[string]any, error) {
	stsURL := "https://sts.cn-north-4.myhuaweicloud.com/v1/oauth2/tokens"
	form := url.Values{}
	form.Set("client_id", clientID)
	form.Set("code", code)
	form.Set("code_verifier", verifier)
	form.Set("grant_type", "authorization_code")
	form.Set("redirect_uri", fmt.Sprintf("http://127.0.0.1:%d%s", port, redirectPath))
	proof, err := cred.DPoPProof(dpopJWK, stsURL)
	if err != nil {
		return nil, fmt.Errorf("生成 DPoP proof 失败: %w", err)
	}
	req, err := http.NewRequest("POST", stsURL, strings.NewReader(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("DPoP", proof)
	req.Header.Set("User-Agent", "CodeArts2api/1.0")
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("换码失败 HTTP %d: %.300s", resp.StatusCode, raw)
	}
	var body map[string]any
	if json.Unmarshal(raw, &body) != nil {
		return nil, fmt.Errorf("换码响应不是 JSON: %.300s", raw)
	}
	// 兼容新旧通道字段名
	creds, _ := body["credentials"].(map[string]any)
	if creds == nil {
		creds, _ = body["credential"].(map[string]any)
	}
	accessKey := jwkStr(creds, "access_key_id", "access")
	secretKey := jwkStr(creds, "secret_access_key", "secret")
	token := jwkStr(creds, "security_token", "securityToken")
	if accessKey == "" || secretKey == "" || token == "" {
		return nil, fmt.Errorf("换码响应缺少凭证: %.300s", raw)
	}
	return map[string]any{
		"user_id":           str(body["user_id"]),
		"user_name":         str(body["user_name"]),
		"domain_id":         str(body["domain_id"]),
		"access_key_id":     accessKey,
		"secret_access_key": secretKey,
		"security_token":    token,
		"expiration":        jwkStr(creds, "expiration", "expires_at"),
		"refresh_token":     str(body["refresh_token"]),
	}, nil
}

// DecodeUserFromRefreshToken 从 refresh_token JWT 的 user_profile 字段解出用户身份。
// refresh_token 是 RS256 JWT，payload 里嵌了 user_profile（无签名 JWT），包含
// principal_id（=user_id）、account_id（=domain_id）、account_name（=user_name）。
// 只解码不验签——用户信息仅用于显示和文件命名。
func DecodeUserFromRefreshToken(refreshToken string) map[string]any {
	parts := strings.Split(refreshToken, ".")
	if len(parts) < 2 {
		return nil
	}
	payloadRaw, err := base64.RawURLEncoding.DecodeString(padB64(parts[1]))
	if err != nil {
		return nil
	}
	var payload map[string]any
	if json.Unmarshal(payloadRaw, &payload) != nil {
		return nil
	}
	userProfile := str(payload["user_profile"])
	if userProfile == "" {
		return nil
	}
	// user_profile 本身也是 JWT（纯 payload，无签名头）
	profileParts := strings.Split(userProfile, ".")
	idx := 0
	if len(profileParts) >= 2 {
		idx = 1
	}
	profileRaw, err := base64.RawURLEncoding.DecodeString(padB64(profileParts[idx]))
	if err != nil {
		return nil
	}
	var profile map[string]any
	if json.Unmarshal(profileRaw, &profile) != nil {
		return nil
	}
	return map[string]any{
		"user_id":   str(profile["principal_id"]),
		"user_name": str(profile["account_name"]),
		"domain_id": str(profile["account_id"]),
	}
}

// SaveAuth 保存凭证到 auths/codearts-{user_id}.json（与 cred 包 loadAuthFiles
// 的键名对齐），返回文件路径。
func SaveAuth(stateDir string, result map[string]any, verifier string, dpopJWK map[string]string) (string, error) {
	dir := filepath.Join(stateDir, "auths")
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	userID := str(result["user_id"])
	if userID == "" {
		userID = "unknown"
	}
	path := filepath.Join(dir, "codearts-"+userID+".json")
	payload := map[string]any{
		"user_id":            userID,
		"user_name":          result["user_name"],
		"domain_id":          result["domain_id"],
		"cloud_dragon_token": result["security_token"],
		"access_key_id":      result["access_key_id"],
		"secret_access_key":  result["secret_access_key"],
		"expiration":         result["expiration"],
		"refresh_token":      result["refresh_token"],
		"code_verifier":      verifier,
		"client_id":          clientID,
		"dpop_private_key":   dpopJWK,
		"updated_at":         time.Now().Unix(),
	}
	raw, err := json.MarshalIndent(payload, "", "  ")
	if err != nil {
		return "", err
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		return "", err
	}
	return path, nil
}

// ── 小工具 ───────────────────────────────────────────────────────────────────

func str(v any) string {
	s, _ := v.(string)
	return s
}

func jwkStr(m map[string]any, keys ...string) string {
	for _, k := range keys {
		if s := str(m[k]); s != "" {
			return s
		}
	}
	return ""
}

// padB64 兼容带 padding 的 base64url（RawURLEncoding 不接受 '='）。
func padB64(s string) string {
	return strings.TrimRight(s, "=")
}
