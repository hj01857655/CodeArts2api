// DPAPI（CryptUnprotectData）与 os_crypt AES-256-GCM 原语。
// 仅 Windows 提供 DPAPI；其他平台编译期给出未实现桩。
package cred

import (
	"crypto/aes"
	"crypto/cipher"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"unicode/utf8"
)

// hasPrefix / trimSpaceBytes 小工具（避免引入 bytes 依赖处的语义漂移）。
func hasPrefix(b []byte, s string) bool {
	return len(b) >= len(s) && string(b[:len(s)]) == s
}

func trimSpaceBytes(b []byte) []byte {
	start, end := 0, len(b)
	for start < end && isSpaceByte(b[start]) {
		start++
	}
	for end > start && isSpaceByte(b[end-1]) {
		end--
	}
	return b[start:end]
}

func isSpaceByte(c byte) bool {
	return c == ' ' || c == '\t' || c == '\n' || c == '\r'
}

// oscryptKey 读取并解密 os_crypt 主密钥（对应 _os_crypt_key）。
// Local State 的 os_crypt.encrypted_key 是 base64("DPAPI" + DPAPI密文)，
// 去掉前缀后用 CryptUnprotectData 解出 AES-256-GCM key。
func oscryptKey(storageDir string) ([]byte, error) {
	raw, err := os.ReadFile(filepath.Join(storageDir, "Local State"))
	if err != nil {
		return nil, nil // 没有 Local State：视为无密钥，不算错误
	}
	var data struct {
		OsCrypt struct {
			EncryptedKey string `json:"encrypted_key"`
		} `json:"os_crypt"`
	}
	if json.Unmarshal(raw, &data) != nil || data.OsCrypt.EncryptedKey == "" {
		return nil, nil
	}
	blob, err := base64.StdEncoding.DecodeString(data.OsCrypt.EncryptedKey)
	if err != nil || !hasPrefix(blob, "DPAPI") {
		return nil, nil
	}
	return dpapiUnprotect(blob[5:])
}

// aesGCMOpen 对应 AESGCM(key).decrypt(nonce, ct, None)：ct 尾部 16 字节是 tag。
func aesGCMOpen(key, nonce, ct []byte) ([]byte, error) {
	block, err := aes.NewCipher(key)
	if err != nil {
		return nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, err
	}
	return gcm.Open(nil, nonce, ct, nil)
}

// utf8Valid 暴露给 rawToBytes（包内统一入口）。
func utf8Valid(b []byte) bool { return utf8.Valid(b) }
