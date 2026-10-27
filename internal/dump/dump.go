// Package dump 请求/响应原文落盘（对应 server.py 的 DUMP_* 与 _dump_save）。
// 只落 body 原文，绝不落 HTTP header：Authorization 里是明文网关 key，且 body
// 需可直接重放。默认 dumps/，DUMP_DIR 改路径，DUMP=0 关闭。
package dump

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const dumpMaxBytes = 512 * 1024 // 单方向落盘上限，防超大响应撑爆磁盘

// Writer 进程级 dump 落盘器。
type Writer struct {
	On       bool
	Dir      string
	MaxFiles int // ≤0 表示不轮转

	// boot 文件名前缀：rid 每进程从 r0001 重新计数，旧版同名 rid 跳重启后
	// 直接覆盖旧证据
	boot string

	mu sync.Mutex
}

// NewWriter 读 DUMP / DUMP_DIR / DUMP_MAX_FILES 三个环境变量。
func NewWriter(stateDir string) *Writer {
	dir := strings.TrimSpace(os.Getenv("DUMP_DIR"))
	if dir == "" {
		dir = filepath.Join(stateDir, "dumps")
	}
	return &Writer{
		On:       strings.TrimSpace(os.Getenv("DUMP")) != "0",
		Dir:      dir,
		MaxFiles: envIntDefault("DUMP_MAX_FILES", 200),
		boot:     time.Now().Format("0102-150405") + fmt.Sprintf("-p%d", os.Getpid()),
	}
}

// Save 把请求/响应原文落盘到 dumps/<boot>.<rid>.<kind>.txt（DUMP=0 时跳过）。
func (w *Writer) Save(rid, kind, data string) {
	if w == nil || !w.On || data == "" {
		return
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	if err := os.MkdirAll(w.Dir, 0o755); err != nil {
		return
	}
	truncated := len(data) > dumpMaxBytes
	if truncated {
		data = data[:dumpMaxBytes]
	}
	name := fmt.Sprintf("%s.%s.%s.txt", w.boot, rid, kind)
	if truncated {
		name += ".truncated"
	}
	if err := os.WriteFile(filepath.Join(w.Dir, name), []byte(data), 0o600); err != nil {
		return
	}
	w.trim()
}

// trim dump 数量超过 MaxFiles 时按 mtime 删最旧（明文提示词会无限增长）。
func (w *Writer) trim() {
	if w.MaxFiles <= 0 {
		return
	}
	entries, err := os.ReadDir(w.Dir)
	if err != nil {
		return
	}
	type entry struct {
		path  string
		mtime time.Time
	}
	files := make([]entry, 0, len(entries))
	for _, e := range entries {
		if e.IsDir() {
			continue
		}
		info, err := e.Info()
		if err != nil {
			continue
		}
		files = append(files, entry{filepath.Join(w.Dir, e.Name()), info.ModTime()})
	}
	if len(files) <= w.MaxFiles {
		return
	}
	sort.Slice(files, func(i, j int) bool { return files[i].mtime.Before(files[j].mtime) })
	for _, stale := range files[:len(files)-w.MaxFiles] {
		_ = os.Remove(stale.path)
	}
}

func envIntDefault(key string, def int) int {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	n := 0
	for _, c := range v {
		if c < '0' || c > '9' {
			return def
		}
		n = n*10 + int(c-'0')
	}
	return n
}
