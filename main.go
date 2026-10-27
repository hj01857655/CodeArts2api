// CodeArts2api 入口。无参数或 serve = 启动代理（对齐 python server.py 的
// __main__ 段）；login = OAuth2 PKCE 浏览器登录（对齐 python lanes/oauth.py，
// 支持 --print-only 服务器模式）。
package main

import (
	"bufio"
	crand "crypto/rand"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"time"

	"codearts2api/internal/benefit"
	"codearts2api/internal/codearts"
	"codearts2api/internal/config"
	"codearts2api/internal/cred"
	"codearts2api/internal/dump"
	"codearts2api/internal/handler"
	"codearts2api/internal/oauth"
	"codearts2api/internal/session"
)

func main() {
	arg := ""
	if len(os.Args) > 1 {
		arg = os.Args[1]
	}
	switch arg {
	case "login":
		os.Exit(runLogin())
	case "serve", "":
		runServe()
	default:
		fmt.Fprintf(os.Stderr, "用法: codearts2api [serve|login [--print-only]]\n未知子命令: %s\n", arg)
		os.Exit(2)
	}
}

// ── serve ────────────────────────────────────────────────────────────────────

func runServe() {
	cfg := config.Load()
	log := handler.NewLogger(cfg.StateDir)

	creds := &cred.Provider{StateDir: cfg.StateDir}
	ben := &benefit.Benefit{Creds: creds, Log: log}
	// 冒烟/调试：CODEARTS_UPSTREAM_BASE 把上游整体指到假地址（含签到通道）
	if cfg.UpstreamBase != "" {
		ben.BaseOverride = cfg.UpstreamBase
	}
	client := codearts.NewClient(cfg, creds, ben, log)

	// 启动时先同步一次真实模型列表（失败则回退到缓存/兜底列表），再打印
	if !client.RefreshModels() {
		client.LoadModelCache()
	}

	dumper := dump.NewWriter(cfg.StateDir)
	srv := &handler.Server{Cfg: cfg, C: client, Ben: ben, Dumper: dumper, Log: log}

	// 凭证横幅
	cc := creds.Get(false)
	if cc != nil && cc.Token != "" {
		source := "CodeArts IDE 会话"
		if cc.AuthFile != "" {
			source = "auths 文件"
		}
		fmt.Printf("凭证来源: %s %s\n", source, cred.TokenNotice(cc))
	} else {
		fmt.Println("凭证来源: 未找到凭证，请运行 codearts2api login 登录或在 CodeArts IDE 中登录")
	}
	addr := fmt.Sprintf("http://%s:%d", cfg.ListenHost, cfg.Port)
	fmt.Printf("CodeArts OpenAI 兼容代理已启动: %s\n", addr)
	fmt.Printf("OpenAI Base URL: %s/v1\n", addr)
	if cfg.APIKey != "" {
		fmt.Println("鉴权: 已启用 API_KEY（Bearer）")
	} else {
		fmt.Println("鉴权: 未设置 API_KEY（无鉴权）")
	}
	if cfg.SessionGateOn {
		fmt.Printf("并发闸门: 本地准入 %d 路（满员回 409），heartbeat %v/%gs\n",
			session.SessionLimit(), session.HeartbeatOnEnv(), session.HeartbeatIntervalEnv().Seconds())
	} else {
		fmt.Println("并发闸门: 已关闭（CODEARTS_SESSION_GATE=0）")
	}
	fmt.Println("可用模型:", client.Supported())
	fmt.Println("按 Ctrl+C 停止")

	if cfg.BenefitAutoClaim {
		// 后台每日签到（对应 benefit_checkin_loop）
		go ben.RunCheckinLoop(log)
	}
	if err := http.ListenAndServe(fmt.Sprintf("%s:%d", cfg.ListenHost, cfg.Port), srv.Handler()); err != nil {
		fmt.Fprintln(os.Stderr, "监听失败:", err)
		os.Exit(1)
	}
}

// ── login（对应 lanes/oauth.py main()）───────────────────────────────────────

func runLogin() int {
	printOnly := false
	for _, a := range os.Args[2:] {
		if a == "--print-only" {
			printOnly = true
		}
	}
	cfg := config.Load()

	ticketID := randHex(16)
	verifier, challenge := oauth.GeneratePKCE()
	dpopJWK, err := oauth.GenerateDPoPKey()
	if err != nil {
		fmt.Println("生成 DPoP 密钥失败:", err)
		return 1
	}

	port := 0
	codeCh := make(chan string, 1)
	server := &http.Server{}
	if !printOnly {
		mux := http.NewServeMux()
		mux.HandleFunc("/oauth/callback", func(w http.ResponseWriter, r *http.Request) {
			q := r.URL.Query()
			code := q.Get("code")
			redirect := q.Get("redirect")
			// portal 首次回调带 redirect，需 307 跳回（登录页链路的一环）
			if code == "" && redirect != "" {
				http.Redirect(w, r, redirect, http.StatusTemporaryRedirect)
				return
			}
			if code != "" {
				select {
				case codeCh <- code:
				default:
				}
				w.Header().Set("Content-Type", "text/html; charset=utf-8")
				_, _ = w.Write([]byte("<h3>登录成功，可关闭此页面。</h3>"))
				return
			}
			w.Header().Set("Content-Type", "text/html; charset=utf-8")
			w.WriteHeader(http.StatusBadRequest)
			_, _ = w.Write([]byte("<h3>登录失败：缺少 code</h3>"))
		})
		ln, err := net.Listen("tcp", "127.0.0.1:0")
		if err != nil {
			fmt.Println("回调端口监听失败:", err)
			return 1
		}
		port = ln.Addr().(*net.TCPAddr).Port
		server = &http.Server{Handler: mux}
		go func() { _ = server.Serve(ln) }()
	}

	loginURL := oauth.BuildAuthorizeURL(ticketID, challenge, port)
	fmt.Println(strings.Repeat("=", 60))
	fmt.Println("  CodeArts Agent 登录（华为云账号）")
	fmt.Println(strings.Repeat("=", 60))
	fmt.Println("登录链接：")
	fmt.Printf("  %s\n\n", loginURL)

	var code string
	if !printOnly {
		openBrowser(loginURL)
		fmt.Println("浏览器已打开，完成登录后自动换取凭证...")
		select {
		case code = <-codeCh:
		case <-time.After(300 * time.Second):
			fmt.Println("登录超时（5 分钟）")
			_ = server.Close()
			return 1
		}
		_ = server.Close()
	} else {
		fmt.Print("打开上面链接完成登录后，将回调 URL 粘贴到这里：")
		line, _ := bufio.NewReader(os.Stdin).ReadString('\n')
		q, err := url.ParseQuery(strings.TrimSpace(line))
		if err == nil {
			if u, err := url.Parse(strings.TrimSpace(line)); err == nil {
				q = u.Query()
			}
		}
		code = q.Get("code")
		if code == "" {
			fmt.Println("回调 URL 中缺少 code")
			return 1
		}
	}

	result, err := oauth.ExchangeCode(nil, code, verifier, port, dpopJWK)
	if err != nil {
		fmt.Println("登录失败:", err)
		return 1
	}
	// STS authorization_code 响应不含用户信息，从 refresh_token JWT 的 user_profile 解出
	if str(result["user_id"]) == "" {
		if info := oauth.DecodeUserFromRefreshToken(str(result["refresh_token"])); info != nil {
			for k, v := range info {
				if str(v) != "" {
					result[k] = v
				}
			}
		}
	}
	path, err := oauth.SaveAuth(cfg.StateDir, result, verifier, dpopJWK)
	if err != nil {
		fmt.Println("凭证保存失败:", err)
		return 1
	}
	fmt.Printf("\n✅ 登录成功：user_id=%s name=%s\n", result["user_id"], result["user_name"])
	fmt.Printf("凭证已保存：%s\n", path)
	if exp := str(result["expiration"]); exp != "" {
		fmt.Printf("STS 有效期至：%s（到期前自动 refresh 续期）\n", exp)
	}
	return 0
}

// ── 小工具 ───────────────────────────────────────────────────────────────────

func str(v any) string {
	s, _ := v.(string)
	return s
}

func randHex(n int) string {
	raw := make([]byte, n)
	_, _ = crand.Read(raw)
	return fmt.Sprintf("%x", raw)
}

// openBrowser Windows 下打开默认浏览器。
func openBrowser(rawURL string) {
	if err := exec.Command("rundll32", "url.dll,FileProtocolHandler", rawURL).Start(); err != nil {
		fmt.Println("（自动打开浏览器失败，请手动复制上面的链接）")
	}
}
