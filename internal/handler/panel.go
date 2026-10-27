// 面板静态页：内置一份 panel.html（//go:embed），状态目录里有同名文件时
// 优先用用户的——内置版只是让 /admin 开箱即用，不依赖手动安装步骤。
package handler

import (
	_ "embed"
	"encoding/json"
	"net/http"
	"os"
	"strings"
)

//go:embed panel.html
var panelBuiltin []byte

// servePanel 输出面板 HTML：状态目录 panel.html > 内置版。
// key 注入页面，前端 fetch 自动携带；面板仅监听本机。
func (s *Server) servePanel(w http.ResponseWriter) {
	raw, err := os.ReadFile(s.Cfg.StateFilePath("panel.html"))
	if err != nil || len(raw) == 0 {
		raw = panelBuiltin
	}
	html := string(raw)
	if s.Cfg.APIKey != "" {
		keyJSON, _ := json.Marshal(s.Cfg.APIKey)
		html = strings.Replace(html, "</head>",
			"<script>window.__API_KEY__ = "+string(keyJSON)+";</script>\n</head>", 1)
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(html))
}
