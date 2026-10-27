//go:build !windows

// 非 Windows 桩：IDE 会话路径依赖 DPAPI，无法解密。
package cred

import "fmt"

func dpapiUnprotect(blob []byte) ([]byte, error) {
	return nil, fmt.Errorf("dpapi only on Windows")
}
