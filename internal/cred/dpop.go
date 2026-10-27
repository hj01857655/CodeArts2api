// DPoP proof JWT（ES256，RFC 9449）：低 S 归一化，签名是 base64url(r||s)
// 整体编码，不能 r、s 分开编码再拼接（base64 按 3 字节分组，跨边界不同）。
package cred

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"encoding/asn1"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"math/big"
	"strings"
	"time"
)

// p256N 曲线阶（低 S 归一化用）。
var p256N, _ = new(big.Int).SetString("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)

type asn1Sig struct {
	R, S *big.Int
}

// DPoPProof 由 JWK（含 d）生成 DPoP proof，绑定 htu=<url>、htm=POST。
// 对应 Python _dpop_jwt。
func DPoPProof(jwk map[string]string, url string) (string, error) {
	dBytes, err := b64uDecode(jwk["d"])
	if err != nil {
		return "", fmt.Errorf("bad jwk d: %w", err)
	}
	xBytes, err := b64uDecode(jwk["x"])
	if err != nil {
		return "", fmt.Errorf("bad jwk x: %w", err)
	}
	yBytes, err := b64uDecode(jwk["y"])
	if err != nil {
		return "", fmt.Errorf("bad jwk y: %w", err)
	}
	d := new(big.Int).SetBytes(dBytes)
	key := &ecdsa.PrivateKey{
		PublicKey: ecdsa.PublicKey{
			Curve: elliptic.P256(),
			X:     new(big.Int).SetBytes(xBytes),
			Y:     new(big.Int).SetBytes(yBytes),
		},
		D: d,
	}

	header := map[string]any{
		"alg": "ES256",
		"typ": "dpop+jwt",
		"jwk": map[string]string{
			"kty": "EC",
			"crv": "P-256",
			"x":   jwk["x"],
			"y":   jwk["y"],
		},
	}
	payload := map[string]any{
		"htm": "POST",
		"htu": url,
		"iat": time.Now().Unix(),
		"jti": jti(),
	}
	hb, _ := json.Marshal(header)
	pb, _ := json.Marshal(payload)
	signingInput := b64uEncode(hb) + "." + b64uEncode(pb)

	// SignASN1 出 DER，再拆出 r、s 做低 S 归一化
	digest := sha256.Sum256([]byte(signingInput))
	der, err := ecdsa.SignASN1(rand.Reader, key, digest[:])
	if err != nil {
		return "", err
	}
	var sig asn1Sig
	if _, err := asn1.Unmarshal(der, &sig); err != nil {
		return "", err
	}
	r, s := sig.R, sig.S
	if s.Cmp(new(big.Int).Div(p256N, big.NewInt(2))) > 0 {
		s.Sub(p256N, s)
	}
	sigRaw := make([]byte, 64)
	r.FillBytes(sigRaw[:32])
	s.FillBytes(sigRaw[32:])
	return signingInput + "." + b64uEncode(sigRaw), nil
}

// jti 16 字节随机 hex（Python secrets.token_hex(16)）。
func jti() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return strings.Repeat("0", 32)
	}
	return fmt.Sprintf("%x", b)
}

func b64uEncode(b []byte) string {
	return base64.RawURLEncoding.EncodeToString(b)
}

func b64uDecode(s string) ([]byte, error) {
	return base64.RawURLEncoding.DecodeString(strings.TrimRight(s, "="))
}
