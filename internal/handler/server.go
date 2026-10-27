// Package handler 对应 Python server.py 的调度面 + routes/ + admin/：
// 路由、鉴权、CORS、rid、in:/done: 日志、请求落盘。引擎件（签名/发送/重试/
// 闸门/模型同步）在 codearts 包，方言翻译在 adapters 包。
package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"iter"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"codearts2api/internal/benefit"
	"codearts2api/internal/codearts"
	"codearts2api/internal/config"
	"codearts2api/internal/dump"
)

// Server 聚合全部依赖；Handler 挂到 http.ServeMux。
type Server struct {
	Cfg    *config.Config
	C      *codearts.Client
	Ben    *benefit.Benefit
	Dumper *dump.Writer
	Log    *slog.Logger

	reqSeq atomic.Int64
}

// cors 对齐 Python _cors。
func cors(w http.ResponseWriter) {
	w.Header().Set("Access-Control-Allow-Origin", "*")
	w.Header().Set("Access-Control-Allow-Headers", "Content-Type, Authorization")
	w.Header().Set("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
}

// nextRID 单调递增请求号。
func (s *Server) nextRID() string {
	return fmt.Sprintf("r%04d", s.reqSeq.Add(1))
}

// Handler 组装路由。Python 的豁免面：/ 与 /admin 与 favicon 不鉴权不打日志
// （面板页与浏览器探测）；OPTIONS 一律免鉴权。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/", s.root) // 面板页 + 兜底 404
	mux.HandleFunc("/admin", s.root)
	mux.HandleFunc("/favicon.ico", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusNotFound)
	})
	mux.HandleFunc("/admin/api/overview", s.adminOverview)
	mux.HandleFunc("/admin/api/checkin", s.adminCheckin)
	mux.HandleFunc("/admin/api/reload-models", s.adminReloadModels)
	mux.HandleFunc("/v1/models", s.models)
	mux.HandleFunc("/v1/models/", s.models) // /v1/models/{id} 单模型详情
	mux.HandleFunc("/v1/chat/completions", s.chat)
	mux.HandleFunc("/v1/responses", s.responses)
	mux.HandleFunc("/v1/messages", s.anthropic)
	return s.middleware(mux)
}

// middleware 鉴权 + rid 分配 + in: 日志 + 请求落盘 + CORS
// （对应 before_request 三件与 after_request）。
func (s *Server) middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cors(w)
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusOK)
			return
		}

		rid := s.nextRID()
		t0 := time.Now()

		// 豁免面：面板页不鉴权不打日志（key 注入页面后前端自动携带）
		exempt := r.URL.Path == "/" || r.URL.Path == "/admin" || r.URL.Path == "/favicon.ico"
		if s.Cfg.APIKey != "" && !exempt {
			auth := r.Header.Get("Authorization")
			token := ""
			if strings.HasPrefix(auth, "Bearer ") {
				token = strings.TrimSpace(auth[7:])
			}
			if token != s.Cfg.APIKey {
				writeJSON(w, http.StatusUnauthorized, map[string]any{
					"error": map[string]any{
						"message": "Invalid API key", "type": "invalid_request_error"}})
				return
			}
		}
		if exempt {
			next.ServeHTTP(w, r)
			return
		}

		var payload map[string]any
		var rawBody []byte
		if r.Body != nil {
			rawBody, _ = io.ReadAll(io.LimitReader(r.Body, 64<<20))
			if len(rawBody) > 0 {
				_ = json.Unmarshal(rawBody, &payload)
			}
		}
		ua := strings.ReplaceAll(strings.ReplaceAll(r.Header.Get("User-Agent"), "\r", " "), "\n", " ")
		if ua == "" {
			ua = "-"
		}
		summary := "n/a"
		if payload != nil {
			msgs, tools := 0, 0
			if l, ok := payload["messages"].([]any); ok {
				msgs = len(l)
			}
			if l, ok := payload["tools"].([]any); ok {
				tools = len(l)
			}
			summary = fmt.Sprintf("model=%v msgs=%d tools=%d stream=%v",
				payload["model"], msgs, tools, payload["stream"])
		}
		s.Log.Info(fmt.Sprintf("[%s] in: %s %s | %s | ua=%s | body=%dB",
			rid, r.Method, r.URL.Path, summary, ua, len(rawBody)))
		s.Dumper.Save(rid, "req", string(rawBody))

		ww := &respRecorder{ResponseWriter: w, status: 200}
		r2 := r.WithContext(withMeta(r.Context(), reqMeta{rid: rid, t0: t0}))
		// Flask 的 request.get_data() 可重复取，Go 读完即消费：回填给路由层
		//（Python 三条路由都各自 get_data()，这里等价保证 body 还能读一次）
		if len(rawBody) > 0 {
			r2.Body = io.NopCloser(bytes.NewReader(rawBody))
		} else {
			r2.Body = http.NoBody
		}
		next.ServeHTTP(ww, r2)
		if !ww.streamed {
			// 非流式：done 行 + 落盘（流式由 streamLog 收尾）
			s.Log.Info(fmt.Sprintf("[%s] done: status=%d %dB %.1fs",
				rid, ww.status, ww.written, time.Since(t0).Seconds()))
			s.Dumper.Save(rid, "resp", ww.buf.String())
		}
	})
}

// respRecorder 记录非流式响应的状态码/字节数/原文。
type respRecorder struct {
	http.ResponseWriter
	status   int
	written  int
	buf      bytes.Buffer
	streamed bool
}

func (r *respRecorder) WriteHeader(code int) {
	if _, ok := r.ResponseWriter.(http.Flusher); ok && strings.HasPrefix(
		r.Header().Get("Content-Type"), "text/event-stream") {
		r.streamed = true
	}
	r.status = code
	r.ResponseWriter.WriteHeader(code)
}

func (r *respRecorder) Write(p []byte) (int, error) {
	if strings.HasPrefix(r.Header().Get("Content-Type"), "text/event-stream") {
		r.streamed = true
	} else if !r.streamed {
		r.buf.Write(p)
	}
	n, err := r.ResponseWriter.Write(p)
	r.written += n
	return n, err
}

func (r *respRecorder) Flush() {
	if f, ok := r.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}

// root 面板页（/ 与 /admin）+ 未注册路径 404。
func (s *Server) root(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path == "/" || r.URL.Path == "/admin" {
		s.servePanel(w)
		return
	}
	writeJSON(w, http.StatusNotFound, map[string]any{
		"error": map[string]any{
			"message": "Not Found: " + r.URL.Path, "type": "not_found_error"}})
}

// ── 请求上下文（rid/t0 传递；生成器消费时请求结构已不可用，先抓成局部值）──────

type reqMeta struct {
	rid string
	t0  time.Time
}

type metaKey struct{}

func withMeta(ctx context.Context, m reqMeta) context.Context {
	return context.WithValue(ctx, metaKey{}, m)
}

func metaFrom(r *http.Request) reqMeta {
	m, _ := r.Context().Value(metaKey{}).(reqMeta)
	return m
}

func ridFrom(r *http.Request) string { return metaFrom(r).rid }

// ── 响应辅助 ─────────────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(marshalCompact(v))
}

// writeUpstreamErr 把 codearts.UpstreamError 写给客户端（错误体已含 CORS）。
func writeUpstreamErr(w http.ResponseWriter, ue *codearts.UpstreamError) {
	writeJSON(w, ue.Status, ue.Body)
}

func marshalCompact(v any) []byte {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return []byte("null")
	}
	return bytes.TrimRight(buf.Bytes(), "\n")
}

func str(v any) string {
	s, _ := v.(string)
	return s
}

// sseHeaders 流式响应头。
func sseHeaders(w http.ResponseWriter) {
	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache, no-transform")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
}

// streamLog 包裹 SSE 行流：TTFT、字节数、原文落盘、done/断开日志
// （对应 Python _stream_log）。
func (s *Server) streamLog(seq iter.Seq[string], rid string, t0 time.Time) iter.Seq[string] {
	return func(yield func(string) bool) {
		total, ttft, seen := 0, time.Duration(0), 0
		var buf []string
		for line := range seq {
			seen++
			if seen == 1 {
				ttft = time.Since(t0)
			}
			total += len(line)
			if s.Dumper.On {
				buf = append(buf, line)
			}
			if !yield(line) {
				s.Log.Warn(fmt.Sprintf("[%s] client 断开: stream %dB %.1fs",
					rid, total, time.Since(t0).Seconds()))
				return
			}
		}
		if s.Dumper.On {
			s.Dumper.Save(rid, "resp", strings.Join(buf, ""))
		}
		s.Log.Info(fmt.Sprintf("[%s] done: stream %dB %.1fs ttft=%s",
			rid, total, time.Since(t0).Seconds(),
			map[bool]string{true: fmt.Sprintf("%.1fs", ttft.Seconds()), false: "-"}[seen > 0]))
	}
}

// writeSSE 把行流写给客户端（带流日志）。
func (s *Server) writeSSE(w http.ResponseWriter, r *http.Request, seq iter.Seq[string]) {
	sseHeaders(w)
	flusher, _ := w.(http.Flusher)
	meta := metaFrom(r)
	for line := range s.streamLog(seq, meta.rid, meta.t0) {
		if _, err := io.WriteString(w, line); err != nil {
			return
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
}

// ── 日志：request.log 2MB×3 轮转 + stdout ────────────────────────────────────

// NewLogger 建 slog logger：同时写 stdout 与 request.log（2MB 轮转，保 3 份）。
func NewLogger(stateDir string) *slog.Logger {
	path := filepath.Join(stateDir, "request.log")
	rw := &rotateWriter{path: path, maxBytes: 2 << 20, backupCount: 3}
	return slog.New(slog.NewTextHandler(io.MultiWriter(os.Stdout, rw), nil))
}

type rotateWriter struct {
	mu          sync.Mutex
	path        string
	maxBytes    int64
	backupCount int
	f           *os.File
	written     int64
}

func (rw *rotateWriter) Write(p []byte) (int, error) {
	rw.mu.Lock()
	defer rw.mu.Unlock()
	if rw.f == nil {
		if err := rw.open(); err != nil {
			return 0, err
		}
	}
	if rw.written+int64(len(p)) > rw.maxBytes {
		rw.rotate()
	}
	n, err := rw.f.Write(p)
	rw.written += int64(n)
	return n, err
}

func (rw *rotateWriter) open() error {
	f, err := os.OpenFile(rw.path, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o600)
	if err != nil {
		return err
	}
	rw.f = f
	if info, err := f.Stat(); err == nil {
		rw.written = info.Size()
	}
	return nil
}

func (rw *rotateWriter) rotate() {
	if rw.f != nil {
		rw.f.Close()
		rw.f = nil
	}
	for i := rw.backupCount - 1; i > 0; i-- {
		os.Rename(fmt.Sprintf("%s.%d", rw.path, i), fmt.Sprintf("%s.%d", rw.path, i+1))
	}
	os.Rename(rw.path, rw.path+".1")
	rw.written = 0
	_ = rw.open()
}
