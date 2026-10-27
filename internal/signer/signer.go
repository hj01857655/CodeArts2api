// Package signer 实现华为云 SDK-HMAC-SHA256 请求签名。
//
// 两个签名形态（对应 Python server.py 的 _signed_request / _hmac_headers）：
//
//   - SignHeaders：按 URL 现算 canonical_uri/query，用于模型同步、心跳、
//     benefit-gateway-config、opengw 等任意路径。host 自动参与签名。
//   - ChatHeaders：固定 POST /api/v2/chat/completions/ 空 query，
//     按 benefit/session_id 附加三个 maas 头与 user-session-id 头。
//
// 两条链路不能混用：区域 API（snap-access /v1/*）的心跳签名要求带 Host，
// 而 chat completions 那条实测不带 Host 也能过——统一都带上没有坏处，
// 关键差异在 canonical_uri 是否带尾斜杠与 query 处理。
package signer

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net/url"
	"sort"
	"strings"
	"time"
)

// SignHeaders 构造一组带 Authorization 的请求头。extra 的键会转小写参与签名
// （华为云规范要求头名一律小写，大写会 APIG 401）。返回的 map 键全小写。
func SignHeaders(ak, sk, token, method, rawURL, body string, extra map[string]string) map[string]string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		parsed = &url.URL{Path: "/"}
	}
	path := parsed.EscapedPath()
	if path == "" {
		path = "/"
	}
	// canonical_uri 带尾斜杠（华为云规范：path 非空则以 / 结尾）
	canonicalURI := path
	if !strings.HasSuffix(canonicalURI, "/") {
		canonicalURI += "/"
	}
	// query：按 (k,v) 排序，保留空值与重复键，转义 safe="-_.~"（对齐 parse_qsl+quote）
	canonicalQuery := canonicalQueryOf(parsed.RawQuery)

	headers := map[string]string{}
	for k, v := range extra {
		headers[strings.ToLower(k)] = fmt.Sprint(v)
	}
	headers["host"] = parsed.Host
	if _, ok := headers["content-type"]; !ok {
		headers["content-type"] = "application/json"
	}
	if token != "" {
		headers["x-security-token"] = token
	}
	return finalize(headers, sk, ak, strings.ToUpper(method), canonicalURI, canonicalQuery, body, timeNow())
}

// ChatHeaders 对应 Python _hmac_headers：仅适用于 POST /api/v2/chat/completions。
// benefit=true 且 modelID 非空时附加 maas_type/model-id/model-name 三个头
// （必须参与签名，否则上游报 "model is not registered"）；sessionID 非空时附加
// user-session-id 头并参与签名（上游靠它把请求归到已 busy 的会话上）。
func ChatHeaders(ak, sk, token, body string, benefit bool, modelID, sessionID string) map[string]string {
	headers := map[string]string{
		"host":         "snap-access.cn-north-4.myhuaweicloud.com",
		"content-type": "application/json",
	}
	if token != "" {
		headers["x-security-token"] = token
	}
	if benefit && modelID != "" {
		headers["maas_type"] = "benefit"
		headers["model-id"] = modelID
		headers["model-name"] = modelID
	}
	if sessionID != "" {
		headers["user-session-id"] = sessionID
	}
	return finalize(headers, sk, ak, "POST", "/api/v2/chat/completions/", "", body, timeNow())
}

// finalize 填 x-sdk-date、计算签名、写入 Authorization（头名转小写返回）。
func finalize(headers map[string]string, sk, ak, method, canonicalURI, canonicalQuery, body, sdkDate string) map[string]string {
	headers["x-sdk-date"] = sdkDate

	names := make([]string, 0, len(headers))
	for k := range headers {
		names = append(names, k)
	}
	sort.Strings(names)
	var ch strings.Builder
	for _, k := range names {
		ch.WriteString(k)
		ch.WriteByte(':')
		ch.WriteString(strings.TrimSpace(headers[k]))
		ch.WriteByte('\n')
	}
	signedHeaders := strings.Join(names, ";")
	payloadHash := sha256Hex(body)
	canonicalRequest := strings.Join([]string{
		method, canonicalURI, canonicalQuery, ch.String(), signedHeaders, payloadHash,
	}, "\n")
	stringToSign := "SDK-HMAC-SHA256\n" + sdkDate + "\n" + sha256Hex(canonicalRequest)
	mac := hmac.New(sha256.New, []byte(sk))
	mac.Write([]byte(stringToSign))
	signature := hex.EncodeToString(mac.Sum(nil))

	lower := make(map[string]string, len(headers)+1)
	for k, v := range headers {
		lower[k] = v
	}
	lower["authorization"] = fmt.Sprintf(
		"SDK-HMAC-SHA256 Access=%s, SignedHeaders=%s, Signature=%s", ak, signedHeaders, signature)
	return lower
}

// canonicalQueryOf 对齐 Python：
//
//	params = sorted(parse_qsl(qs, keep_blank_values=True))
//	"&".join(f"{quote(k)}={quote(v)}")
//
// parse_qsl 把 + 当空格、%XX 解码；quote 再按 safe="-_.~" 转义（空格→%20）。
func canonicalQueryOf(rawQuery string) string {
	if rawQuery == "" {
		return ""
	}
	values, err := url.ParseQuery(rawQuery)
	if err != nil {
		// 畸形 query 退化为原样参与签名（上游同样拒，调用方会看到 401 而非 panic）
		return rawQuery
	}
	type kv struct{ k, v string }
	var pairs []kv
	for k, vs := range values {
		for _, v := range vs {
			pairs = append(pairs, kv{k, v})
		}
	}
	sort.Slice(pairs, func(i, j int) bool {
		if pairs[i].k != pairs[j].k {
			return pairs[i].k < pairs[j].k
		}
		return pairs[i].v < pairs[j].v
	})
	parts := make([]string, 0, len(pairs))
	for _, p := range pairs {
		parts = append(parts, awsURIEncode(p.k)+"="+awsURIEncode(p.v))
	}
	return strings.Join(parts, "&")
}

// awsURIEncode 按 safe="-_.~" 百分号转义（RFC 3986 unreserved 的 AWS 子集）。
// 不用 url.QueryEscape：它把空格编成 +、转义范围也不同。
func awsURIEncode(s string) string {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') ||
			c == '-' || c == '_' || c == '.' || c == '~' {
			b.WriteByte(c)
		} else {
			fmt.Fprintf(&b, "%%%02X", c)
		}
	}
	return b.String()
}

func sha256Hex(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// timeNow 独立变量：测试注入固定时间验证签名稳定性。
var timeNow = func() string {
	return time.Now().UTC().Format("20060102T150405Z")
}
