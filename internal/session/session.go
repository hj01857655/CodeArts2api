// Package session 对应 Python lanes/session.py：CodeArts 并发闸门。
//
// 上游对每账号只放 3 路并发对话，放不下报 TM.00001041。闸门不认「请求结束」，
// 只认 PUT /snap-manager/v1/chat-session/heartbeat?status=busy|idle：
//
//	ChatSession  一次上游请求的生命周期：请求前 busy，流式期间每 30s 续期，
//	             收尾 idle。session id 由我们自造（UUIDv4 去连字符），
//	             绝不复用 IDE 的——idle 是注销语义。
//	SessionGate  本地准入闸：按账号计在途请求数，满员让调用方回 409
//	             （不是 429——429 会让客户端进长冷却，而这个条件几十毫秒
//	             就会自己消失）。
//
// 闸门 key 是 sha256(base_url + "\n" + account_id)。不能用 AK 当身份：
// AK 每次续期都换，会悄悄重置 3 路上限。account_id 由 cred 包给。
//
// 参考实现的五条规则逐条落实（对应处有注释）：
//  1. 周期 busy 续期失败不释放槽位
//  2. 首次 busy 非 200 仍要注销自己造的那个 id
//  3. 只注销自己造的 id
//  4. idle 只发一次，且在最后一个在途 busy 落地之后
//  5. 本地准入满员回 409
package session

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"log/slog"
	"os"
	"strconv"
	"sync"
	"time"
)

// HeartbeatPath 心跳端点路径（挂在 snap-access 域）。
const HeartbeatPath = "/snap-manager/v1/chat-session/heartbeat"

// GateStatus 本地闸门满员时的 HTTP 状态（409 语义见包注释）。
const GateStatus = 409

// Sender 发一次心跳；返回是否被上游接受（HTTP 200 且 body {"status":"ok"}）。
// 实现在 engine 包（走通用签名请求），此处解耦便于测试。
type Sender func(status, sessionID string) (accepted bool)

// heartbeatInterval / heartbeatOn 对齐 Python 的 env 读取。
// 心跳整体开关：关掉仍发 user-session-id（那才是把请求绑到会话上的关键），
// 只是不主动 busy/idle——上游改接口时留的后路。
func heartbeatInterval() time.Duration {
	if v, err := strconv.ParseFloat(os.Getenv("CODEARTS_HEARTBEAT_INTERVAL"), 64); err == nil && v > 0 {
		return time.Duration(v * float64(time.Second))
	}
	return 30 * time.Second
}

func heartbeatOn() bool {
	switch os.Getenv("CODEARTS_SESSION_HEARTBEAT") {
	case "0", "false", "False", "no", "":
		return false
	}
	return true
}

// SessionLimit 每账号并发上限（env 可调，下限 1）。
func SessionLimit() int {
	n, err := strconv.Atoi(os.Getenv("CODEARTS_SESSION_LIMIT"))
	if err != nil || n < 1 {
		return 3
	}
	return n
}

// NewSessionID 自造 32 位十六进制 session id（对齐 IDE 形状：UUIDv4 去连字符）。
func NewSessionID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%032x", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}

// ChatSession 一次上游请求的心跳会话：busy →（每 30s 续期）→ idle。
type ChatSession struct {
	SessionID string
	send      Sender
	log       *slog.Logger
	rid       string
	interval  time.Duration

	ioLock  sync.Mutex
	lock    sync.Mutex
	busy    int // 已成功的 busy 次数；>0 才需要 idle 收尾
	misses  int
	closing bool
	timer   *time.Timer
}

// NewChatSession rid 只为把心跳日志归到具体请求上；心跳是后台行为，
// rid 缺失不报警。
func NewChatSession(send Sender, log *slog.Logger, rid string) *ChatSession {
	return &ChatSession{
		SessionID: NewSessionID(),
		send:      send,
		log:       log,
		rid:       rid,
		interval:  heartbeatInterval(),
	}
}

func (c *ChatSession) prefix() string {
	rid := ""
	if c.rid != "" {
		rid = "[" + c.rid + "] "
	}
	if len(c.SessionID) > 8 {
		return rid + "[session " + c.SessionID[:8] + "] "
	}
	return rid + "[session " + c.SessionID + "] "
}

func (c *ChatSession) warn(format string, args ...any) {
	if c.log != nil {
		c.log.Warn(c.prefix()+format, args...)
	}
}

func (c *ChatSession) err(format string, args ...any) {
	if c.log != nil {
		c.log.Error(c.prefix()+format, args...)
	}
}

// post 发一次心跳。心跳之间串行（ioLock），否则 end() 的 idle 可能插到在途
// busy 之前变成「先注销后登记」，槽位又悬空一个（规则 4）。closing 判断必须
// 在 ioLock 里面：放在外面的话 end() 已置 closing 并拿到锁，一个刚要发 busy
// 的 renew 会照发不误——idle 之后又来 busy，上游重新登记这个 session 而没人
// 再注销它，槽位泄漏。
func (c *ChatSession) post(status string) bool {
	c.ioLock.Lock()
	defer c.ioLock.Unlock()
	if c.closing && status == "busy" {
		return false
	}
	if !c.send(status, c.SessionID) {
		c.warn("heartbeat %s 未被接受", status)
		return false
	}
	return true
}

// schedule 排下一次续期。
func (c *ChatSession) schedule() {
	c.lock.Lock()
	if c.closing {
		c.lock.Unlock()
		return
	}
	if c.timer != nil {
		c.timer.Stop()
	}
	c.lock.Unlock()
	timer := time.AfterFunc(c.interval, c.renew)
	c.lock.Lock()
	if c.closing {
		timer.Stop()
		c.lock.Unlock()
		return
	}
	c.timer = timer
	c.lock.Unlock()
}

// renew 周期续期。失败不释放槽位（规则 1）：一次网络抖动不该把在途请求废掉，
// 收尾时照常 idle。连续失败达到阈值 escalate 一次日志——上游大概率已回收该
// session，真出现 TM.00001041 时这条日志就是原因。
func (c *ChatSession) renew() {
	c.lock.Lock()
	if c.closing {
		c.lock.Unlock()
		return
	}
	c.lock.Unlock()
	ok := c.post("busy")
	c.lock.Lock()
	if c.closing {
		c.lock.Unlock()
		return
	}
	if ok {
		c.busy++
		c.misses = 0
	} else {
		c.misses++
		if c.misses >= 3 {
			misses := c.misses
			c.misses = 0
			c.lock.Unlock()
			c.err("连续 %d 次续期失败，上游可能已回收该 session；若随后出现 TM.00001041 即此因", misses)
			c.schedule()
			return
		}
	}
	c.lock.Unlock()
	c.schedule()
}

// Begin 请求前登记 busy。失败返回 false 但不阻塞请求；仍要注销自己造的 id
// （规则 2）：上游可能已把它记成半开会话，不注销就白占一个槽。
func (c *ChatSession) Begin() bool {
	if !c.post("busy") {
		c.post("idle")
		return false
	}
	c.lock.Lock()
	c.busy++
	c.misses = 0
	c.lock.Unlock()
	c.schedule()
	return true
}

// End 收尾：停续期线程，发且只发一次 idle（规则 4 由 ioLock 保证）。
// 放在 finally 语义里调用，重复调用是空操作。
func (c *ChatSession) End() {
	c.lock.Lock()
	c.closing = true
	timer := c.timer
	c.timer = nil
	needIdle := c.busy > 0
	c.busy = 0
	c.lock.Unlock()
	if timer != nil {
		timer.Stop()
	}
	if needIdle {
		c.post("idle")
	}
}

// Gate 本地准入闸：按账号计在途上游请求数。
type Gate struct {
	Limit  int
	mu     sync.Mutex
	counts map[string]int
}

// NewGate limit 下限 1。
func NewGate(limit int) *Gate {
	if limit < 1 {
		limit = 1
	}
	return &Gate{Limit: limit, counts: map[string]int{}}
}

// KeyFor 闸门 key。换 base_url 或换账号都是另一个门口，不互相抢额度。
func KeyFor(baseURL, accountID string) string {
	sum := sha256.Sum256([]byte(baseURL + "\n" + accountID))
	return hex.EncodeToString(sum[:])
}

// InUse 当前在途数（排障/拒绝时报数用）。
func (g *Gate) InUse(key string) int {
	g.mu.Lock()
	defer g.mu.Unlock()
	return g.counts[key]
}

func (g *Gate) acquire(key string) bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.counts[key] >= g.Limit {
		return false
	}
	g.counts[key]++
	return true
}

func (g *Gate) release(key string) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if used := g.counts[key] - 1; used > 0 {
		g.counts[key] = used
	} else {
		// 归零就删掉：否则长时间跑下来堆满死 key
		delete(g.counts, key)
	}
}

// Snapshot 排障用：各闸门口在途数（面板 /admin/api/overview 消费）。
func (g *Gate) Snapshot() map[string]any {
	g.mu.Lock()
	inUse := make(map[string]int, len(g.counts))
	for k, v := range g.counts {
		inUse[k] = v
	}
	g.mu.Unlock()
	return map[string]any{
		"limit":     g.Limit,
		"in_use":    inUse,
		"heartbeat": heartbeatOn(),
		"interval":  heartbeatInterval().Seconds(),
	}
}

// Slot 一次上游请求持有的闸门槽位（含心跳会话）。Denied 非空表示没占到槽，
// 文本即原因，此时不要发上游请求。Release 幂等，放心放 finally。
type Slot struct {
	Denied    string
	SessionID string
	Key       string

	chat     *ChatSession
	gate     *Gate
	released bool
	mu       sync.Mutex
}

// OK 是否占到槽。
func (s *Slot) OK() bool { return s.Denied == "" }

// Release 还槽 + 注销心跳。重复调用空操作（读 body 与 close 都会调）。
func (s *Slot) Release() {
	s.mu.Lock()
	if s.released {
		s.mu.Unlock()
		return
	}
	s.released = true
	s.mu.Unlock()
	if s.chat != nil {
		s.chat.End()
	}
	if s.gate != nil {
		s.gate.release(s.Key)
	}
}

// Acquire 占一个闸门槽位并登记 busy。满员返回 Denied 的 Slot；
// busy 登记失败不阻断请求，只记日志。
func Acquire(baseURL, accountID string, gate *Gate, send Sender, log *slog.Logger, rid string) *Slot {
	key := KeyFor(baseURL, accountID)
	if !gate.acquire(key) {
		// 不塞 gate：这个槽位压根没占到，Release 不该去动计数
		return &Slot{
			Denied: fmt.Sprintf("本机上游并发已满（%d 路）", gate.Limit),
			Key:    key,
		}
	}
	if !heartbeatOn() {
		return &Slot{SessionID: NewSessionID(), Key: key, gate: gate}
	}
	chat := NewChatSession(send, log, rid)
	slot := &Slot{SessionID: chat.SessionID, Key: key, chat: chat, gate: gate}
	chat.Begin() // 心跳坏了不该让请求失败（Begin 内部已兜底）
	return slot
}

// HeartbeatOnEnv / HeartbeatIntervalEnv 启动横幅展示用（engine 与 main 分读
// 环境变量的口径必须一致，收敛到这里）。
func HeartbeatOnEnv() bool { return heartbeatOn() }

func HeartbeatIntervalEnv() time.Duration { return heartbeatInterval() }
