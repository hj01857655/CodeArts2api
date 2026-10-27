//go:build windows

// Windows DPAPI CryptUnprotectData 封装。
package cred

import (
	"fmt"
	"syscall"
	"unsafe"
)

var (
	crypt32       = syscall.NewLazyDLL("crypt32.dll")
	procUnprotect = crypt32.NewProc("CryptUnprotectData")
	kernel32      = syscall.NewLazyDLL("kernel32.dll")
	procLocalFree = kernel32.NewProc("LocalFree")
)

// dataBlob 对应 CRYPT_INTEGER_BLOB。
type dataBlob struct {
	cbData uint32
	pbData *byte
}

// dpapiUnprotect 用当前用户作用域的 DPAPI 解密（对应 _dpapi_unprotect）。
func dpapiUnprotect(blob []byte) ([]byte, error) {
	if len(blob) == 0 {
		return nil, fmt.Errorf("dpapi: empty blob")
	}
	buf := make([]byte, len(blob))
	copy(buf, blob)
	in := dataBlob{cbData: uint32(len(buf)), pbData: &buf[0]}
	var out dataBlob
	r1, _, _ := procUnprotect.Call(
		uintptr(unsafe.Pointer(&in)),
		0, 0, 0, 0, 0,
		uintptr(unsafe.Pointer(&out)),
	)
	if r1 == 0 {
		return nil, fmt.Errorf("CryptUnprotectData failed")
	}
	defer procLocalFree.Call(uintptr(unsafe.Pointer(out.pbData)))
	plain := make([]byte, out.cbData)
	copy(plain, unsafe.Slice(out.pbData, out.cbData))
	return plain, nil
}
