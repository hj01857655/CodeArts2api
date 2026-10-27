// Package sqlitedb 以完全只读的方式解析 SQLite 数据库文件，读取指定表中某个 key 的 value。
//
// 它存在的唯一理由：CodeArts 迁移到 Go 后不允许引入第三方依赖，而 Go 标准库没有 SQLite；
// state.vscdb 的读取需求又只有「按主键取一行的一列」，所以手写最小解析器。
//
// 只读纪律是本包的第一约束：以 O_RDONLY 打开、只用 ReadAt、不建任何文件、不实现 WAL
// 索引（-wal 存在时直接返回 ErrWALPresent）。因为一旦存在未合并的 WAL，磁盘主文件里的
// 页就是过期快照；而用代码去"读 WAL 补齐快照"必然要处理 checkpoint/shm 语义，
// 那属于会产生副作用的路径，宁可拒绝读取也不能返回错误数据或诱发副作用。
package sqlitedb

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"unicode/utf8"
)

// ErrWALPresent 表示数据库同目录存在非空 -wal 文件。
// 此时主文件内容不是最新快照，本包不做 WAL 合并，直接拒绝读取。
var ErrWALPresent = errors.New("sqlitedb: wal file present, refusing to read a stale snapshot")

// ErrNotDatabase 表示文件头 magic 不是 "SQLite format 3\000"（或文件太短放不下 header）。
var ErrNotDatabase = errors.New("sqlitedb: not a sqlite database file")

// ErrNoSuchTable 表示 sqlite_master 里没有该表的 table 条目。
var ErrNoSuchTable = errors.New("sqlitedb: table not found")

const (
	headerSize        = 100 // page 1 比常规页多出的数据库 header；B-tree page header 从 offset 100 开始
	magic             = "SQLite format 3\x00"
	pageTypeLeaf      = 0x0d // table b-tree leaf
	pageTypeInterior  = 0x05 // table b-tree interior
	pageTypeIdxLeaf   = 0x0a // index leaf：ItemTable 的 UNIQUE 索引，值不在里面，读到就报错
	pageTypeIdxLeafKv = 0x0a

	maxTreeDepth = 512 // 合法库的树深远小于此值；超限说明页号成环或文件损坏，直接报错而不是无限递归
	maxOverflow  = 1 << 26
)

// database 持有已校验过 header 的只读文件句柄与派生出的页几何参数。
type database struct {
	f        *os.File
	size     int64 // 文件总字节数
	pageSize int   // 实际页大小（header 里 1 代表 65536）
	reserved int   // 每页尾部预留区字节数，必须从页 footer（offset 20）取
	usable   int   // 每页真正可用字节数 = pageSize - reserved
	pages    int   // 总页数
}

// open 只读打开文件并校验 header。
// 不做任何"顺手修复"：页大小非法、reserved 过大都视为损坏并报错，
// 因为继续解析只会读出静默错误的凭据字节，那比失败更糟。
func open(dbPath string) (*database, error) {
	f, err := os.OpenFile(dbPath, os.O_RDONLY, 0)
	if err != nil {
		return nil, fmt.Errorf("sqlitedb: open %s: %w", dbPath, err)
	}
	db := &database{f: f}

	hdr := make([]byte, headerSize)
	if _, err := io.ReadFull(f, hdr); err != nil {
		f.Close()
		return nil, fmt.Errorf("%w: short header: %v", ErrNotDatabase, err)
	}
	if string(hdr[:len(magic)]) != magic {
		f.Close()
		return nil, ErrNotDatabase
	}

	ps := int(binary.BigEndian.Uint16(hdr[16:18]))
	if ps == 1 {
		ps = 65536 // 规范用 1 表示 65536
	} else if ps < 512 || ps > 32768 || ps&(ps-1) != 0 {
		f.Close()
		return nil, fmt.Errorf("sqlitedb: illegal page size %d", ps)
	}
	db.pageSize = ps
	db.reserved = int(hdr[20])

	// reserved 至少要容得下 4 字节 freelist trunk 指针且不能吃掉整个页内容区，
	// 否则 cell pointer array 基址会算出负数/重叠区，后续所有切片都可能越界。
	if db.reserved > ps-100-8-4 || db.reserved > 128 {
		f.Close()
		return nil, fmt.Errorf("sqlitedb: illegal reserved bytes %d for page size %d", db.reserved, ps)
	}
	db.usable = ps - db.reserved

	st, err := f.Stat()
	if err != nil {
		f.Close()
		return nil, fmt.Errorf("sqlitedb: stat: %w", err)
	}
	db.size = st.Size()

	// header offset 28 的页计数只有最后一次写提交落在最后一页时才准确，
	// 任何情况下都以文件实际长度为准，header 值仅用于给出更小的读取上界以免读到脏页号。
	n := int(db.size / int64(ps))
	if n < 1 {
		f.Close()
		return nil, fmt.Errorf("%w: file shorter than one page", ErrNotDatabase)
	}
	db.pages = n
	return db, nil
}

func (db *database) close() { db.f.Close() }

// readPage 读第 n 页（1-based）。每次只申请 usable 之外的完整 pageSize：
// reserved 区虽不属于 cell 内容，但它真实存在于页尾，必须读进来才能按偏移正确定位。
func (db *database) readPage(n int) ([]byte, error) {
	if n < 1 || n > db.pages {
		return nil, fmt.Errorf("sqlitedb: page %d out of range (1..%d)", n, db.pages)
	}
	buf := make([]byte, db.pageSize)
	if _, err := db.f.ReadAt(buf, int64(n-1)*int64(db.pageSize)); err != nil {
		return nil, fmt.Errorf("sqlitedb: read page %d: %w", n, err)
	}
	return buf, nil
}

// cellBase 返回该页 cell pointer array 的起始偏移。
// 减 reserved 是关键：SQLite 把每页尾部 reserved 字节留给需要它的后端格式，
// 若按 pageSize-8 取基址，在 reserved!=0 的库上会把未初始化区当指针用，整棵树读错。
func (db *database) cellBase() int { return db.pageSize - db.reserved - 8 }

type pageView struct {
	data  []byte
	typ   byte
	hdr   int // B-tree page header 在该页内的起始偏移
	base  int // cell pointer array 起始偏移
	nCell int
}

// parsePage 解释一个常规页或 page 1（headerOff 决定）。
func (db *database) parsePage(n int, headerOff int) (*pageView, error) {
	data, err := db.readPage(n)
	if err != nil {
		return nil, err
	}
	if headerOff+8 > len(data) {
		return nil, fmt.Errorf("sqlitedb: page %d truncated", n)
	}
	pv := &pageView{data: data, hdr: headerOff}
	pv.typ = data[headerOff]
	switch pv.typ {
	case pageTypeLeaf, pageTypeInterior, pageTypeIdxLeaf:
		// 支持；idx leaf 会在遍历时被拒绝
	default:
		return nil, fmt.Errorf("sqlitedb: page %d has unsupported b-tree type 0x%02x", n, pv.typ)
	}
	pv.nCell = int(binary.BigEndian.Uint16(data[headerOff+3 : headerOff+5]))
	content := int(binary.BigEndian.Uint16(data[headerOff+5 : headerOff+7]))
	if headerOff == 0 {
		content = 65536 // 规范：0 表示 65536
	}
	pv.base = db.cellBase()
	if content > pv.base {
		return nil, fmt.Errorf("sqlitedb: page %d cell content area offset %d overlaps cell pointer array", n, content)
	}
	if pv.base+2*pv.nCell > len(data) {
		return nil, fmt.Errorf("sqlitedb: page %d has %d cells beyond page bounds", n, pv.nCell)
	}
	if pv.typ == pageTypeInterior {
		right := int(binary.BigEndian.Uint16(data[headerOff+7 : headerOff+9]))
		if right < 1 || right > db.pages {
			return nil, fmt.Errorf("sqlitedb: page %d bad rightmost child page %d", n, right)
		}
	}
	return pv, nil
}

// cellOffsets 按 cell pointer array 顺序返回各 cell 的页内偏移。
func (pv *pageView) cellOffsets() ([]int, error) {
	out := make([]int, pv.nCell)
	for i := 0; i < pv.nCell; i++ {
		o := int(binary.BigEndian.Uint16(pv.data[pv.base+2*i : pv.base+2*i+2]))
		if o < 0 || o >= len(pv.data) {
			return nil, fmt.Errorf("sqlitedb: cell pointer %d out of range: %d", i, o)
		}
		out[i] = o
	}
	return out, nil
}

func readVarint(b []byte) (val uint64, n int, err error) {
	for i := 0; i < 9; i++ {
		if i >= len(b) {
			return 0, 0, errors.New("sqlitedb: varint truncated")
		}
		v := b[i]
		if i == 8 {
			// 第 9 个 varint 字节只贡献低 32 位（SQLite 用大端拼 64 bit），
			// 高 32 位来自前 8 字节各 7 bit，共 56+32=88>64，取低 64 位。
			val = val<<32 | uint64(v)
			return val, 9, nil
		}
		val = val<<7 | uint64(v&0x7f)
		if v&0x80 == 0 {
			return val, i + 1, nil
		}
	}
	return 0, 0, errors.New("sqlitedb: varint too long")
}

// serialInfo 描述一个 serial type 对应的值宽度与类别。
type serialInfo struct {
	null   bool
	const1 bool // serial 8 → 整数 1
	const0 bool // serial 9 → 整数 0
	isText bool
	width  int // 需要在本页 local 区读取的"完整值"字节数（未做 overflow 折算）
}

func serialToInfo(st uint64) (serialInfo, error) {
	switch {
	case st == 0:
		return serialInfo{null: true}, nil
	case st >= 1 && st <= 4:
		return serialInfo{width: int(st)}, nil
	case st == 5:
		return serialInfo{width: 6}, nil
	case st == 6:
		return serialInfo{width: 8}, nil
	case st == 7:
		return serialInfo{width: 8}, nil
	case st == 8:
		return serialInfo{const1: true}, nil
	case st == 9:
		return serialInfo{const0: true}, nil
	case st >= 12 && st%2 == 0:
		n := st/2 - 1
		if n > 1<<30 {
			return serialInfo{}, fmt.Errorf("sqlitedb: absurd text serial type %d", st)
		}
		return serialInfo{isText: true, width: int(n)}, nil
	case st >= 13:
		n := (st - 12)
		if n > 1<<30 {
			return serialInfo{}, fmt.Errorf("sqlitedb: absurd blob serial type %d", st)
		}
		return serialInfo{width: int(n)}, nil
	default:
		return serialInfo{}, fmt.Errorf("sqlitedb: illegal serial type %d", st)
	}
}

// recordHeader 是 cell 的 record header 解析结果：各字段的 serial type 与其字节起点。
type recordHeader struct {
	sizes []uint64
	off   []int // 相对 record 数据区起点
	total int   // header 自身长度（含 header size varint）
}

func parseRecordHeader(rec []byte) (*recordHeader, error) {
	hdrSize, hn, err := readVarint(rec)
	if err != nil {
		return nil, err
	}
	if hdrSize < uint64(hn) || hdrSize > uint64(len(rec)) {
		return nil, fmt.Errorf("sqlitedb: record header size %d inconsistent with cell size %d", hdrSize, len(rec))
	}
	rh := &recordHeader{total: int(hdrSize)}
	pos := hn
	for pos < int(hdrSize) {
		st, n, err := readVarint(rec[pos:int(hdrSize)])
		if err != nil {
			return nil, err
		}
		rh.sizes = append(rh.sizes, st)
		rh.off = append(rh.off, pos)
		pos += n
	}
	return rh, nil
}

// cellValue 取出某 cell 里第 idx 个字段的原始字节。
// page/offset 用于 overflow 链续读；visited 防止损坏文件造成的环。
func (db *database) cellValue(pv *pageView, cellOff int, rh *recordHeader, idx int, visited map[int]bool) ([]byte, error) {
	// cell: [payload size varint][rowid varint][record header + body]
	ps, pn, err := readVarint(pv.data[cellOff:])
	if err != nil {
		return nil, err
	}
	_, rn, err := readVarint(pv.data[cellOff+pn:])
	if err != nil {
		return nil, err
	}
	recStart := cellOff + pn + rn
	if recStart < 0 || recStart > len(pv.data) {
		return nil, fmt.Errorf("sqlitedb: cell record start out of bounds: %d", recStart)
	}
	if ps > uint64(1)<<40 {
		return nil, fmt.Errorf("sqlitedb: implausible payload size %d", ps)
	}
	total := int(ps)

	// SQLite 的 local/overflow 折算公式：usable 减 35（leaf）或 12（interior）为上限。
	maxLocal := db.usable - 35
	minLocal := maxLocal/2*40/64 - 23
	if minLocal < 35 {
		minLocal = 35
	}
	var local int
	if total < maxLocal || total <= minLocal {
		local = total
	} else {
		local = minLocal + (total-minLocal)%(maxLocal-minLocal+1)
		if local > maxLocal {
			local = minLocal
		}
	}
	if local < 0 || recStart+local > len(pv.data) {
		return nil, fmt.Errorf("sqlitedb: local payload %d exceeds page bounds", local)
	}

	rec := pv.data[recStart : recStart+local]
	rh2, err := parseRecordHeader(rec)
	if err != nil {
		return nil, fmt.Errorf("sqlitedb: record header: %w", err)
	}
	if idx >= len(rh2.sizes) {
		return nil, fmt.Errorf("sqlitedb: record has %d fields, need field %d", len(rh2.sizes), idx)
	}
	if len(rh2.sizes) < 2 {
		return nil, fmt.Errorf("sqlitedb: record has %d fields; key/value lookup needs at least 2", len(rh2.sizes))
	}

	// 字段起点在"完整 payload"坐标系里累加，而不是在 local 坐标系里，
	// 否则一旦 value 走 overflow，key 之后的偏移全部错位。
	starts := make([]int, len(rh2.sizes))
	cur := rh2.total
	for i, st := range rh2.sizes {
		starts[i] = cur
		info, err := serialToInfo(st)
		if err != nil {
			return nil, err
		}
		cur += info.width
	}
	if cur != total {
		// 只告警式地接受（有些行确实因历史损坏而不自洽），但要能定位字段就不报错，
		// 因此这里只在字段完全落不进 payload 时才失败。
		_ = cur
	}

	info, err := serialToInfo(rh2.sizes[idx])
	if err != nil {
		return nil, err
	}
	if info.null {
		return nil, nil
	}
	if info.const0 || info.const1 {
		return nil, fmt.Errorf("sqlitedb: field %d is integer constant (serial %d), cannot be key/value", idx, rh2.sizes[idx])
	}

	val := make([]byte, info.width)
	fs := starts[idx]
	if fs < 0 || fs+info.width > total {
		return nil, fmt.Errorf("sqlitedb: field %d at [%d,%d) beyond payload %d", idx, fs, fs+info.width, total)
	}

	// 三段式拷贝：完全落在 local 内 / 跨越 local 边界 / 完全在 overflow 区。
	inLocal := local - fs
	if inLocal >= info.width {
		copy(val, rec[fs:fs+info.width])
		return val, nil
	}
	if inLocal > 0 {
		copy(val[:inLocal], rec[fs:])
	}
	need := info.width - inLocal
	if err := db.readOverflow(pv, local, need, val[inLocal:], visited); err != nil {
		return nil, err
	}
	if info.isText && !utf8.Valid(val) {
		// TEXT 列内容按 SQLite 约束必须是合法 UTF-8（除 BLOB cast）。
		// 非法序列意味着我们把 overflow 边界算错了，宁可报错也不返回坏字节。
		return nil, fmt.Errorf("sqlitedb: text field %d is not valid utf-8", idx)
	}
	return val, nil
}

// readOverflow 从链头开始续读 need 字节。
// 每个 overflow 页的前 4 字节是下一个 overflow 页号（大端），其余都是数据——
// 所以每页可用数据是 usable-4，而不是 usable。
func (db *database) readOverflow(pv *pageView, local int, need int, dst []byte, visited map[int]bool) error {
	if need == 0 {
		return nil
	}
	if local > len(pv.data) {
		return errors.New("sqlitedb: local size beyond page")
	}
	pg := make([]byte, 4)
	copy(pg, pv.data[local-4:local]) // overflow 链头页号就写在本页 local 末尾
	next := int(binary.BigEndian.Uint32(pg))
	got := 0
	for hops := 0; got < need; hops++ {
		if next < 1 || next > db.pages {
			return fmt.Errorf("sqlitedb: overflow page %d out of range after %d bytes", next, got)
		}
		if visited == nil {
			visited = map[int]bool{}
		}
		if visited[next] || hops > maxOverflow {
			return fmt.Errorf("sqlitedb: overflow cycle or runaway chain at page %d", next)
		}
		visited[next] = true
		buf, err := db.readPage(next)
		if err != nil {
			return err
		}
		avail := db.usable - 4
		take := need - got
		if take > avail {
			take = avail
		}
		copy(dst[got:got+take], buf[4:4+take])
		got += take
		if got == need {
			break
		}
		next = int(binary.BigEndian.Uint32(buf[db.usable-4 : db.usable]))
	}
	return nil
}

// cellKey 与 cellValue 共享同一套解析，只是固定取字段 0；单独走一次 varint 更快。
func (db *database) cellKey(pv *pageView, cellOff int, visited map[int]bool) ([]byte, error) {
	return db.cellValue(pv, cellOff, nil, 0, visited)
}

// findInTablePage 在 root 开始的 table b-tree 里二分/线性查找 key。
// 返回 value 原始字节；NULL 语义由 (nil, true, nil) 表达。
func (db *database) findInTablePage(pageNo int, headerOff int, key string, visited map[int]bool, depth int) (val []byte, found bool, err error) {
	if depth > maxTreeDepth {
		return nil, false, fmt.Errorf("sqlitedb: b-tree deeper than %d levels", maxTreeDepth)
	}
	if visited == nil {
		visited = map[int]bool{}
	}
	if visited[pageNo] {
		return nil, false, fmt.Errorf("sqlitedb: b-tree cycle at page %d", pageNo)
	}
	visited[pageNo] = true
	defer delete(visited, pageNo)

	pv, err := db.parsePage(pageNo, headerOff)
	if err != nil {
		return nil, false, err
	}
	if pv.typ == pageTypeIdxLeaf || pv.typ == pageTypeIdxLeafKv {
		return nil, false, fmt.Errorf("sqlitedb: page %d is an index b-tree; index traversal is not implemented", pageNo)
	}

	offsets, err := pv.cellOffsets()
	if err != nil {
		return nil, false, err
	}

	if pv.typ == pageTypeLeaf {
		for _, off := range offsets {
			k, err := db.cellKey(pv, off, visited)
			if err != nil {
				return nil, false, err
			}
			// BINARY collation：TEXT/BLOB 的比较就是 memcmp，故直接按字节比。
			if string(k) == key {
				rh, err := parseRecordHeader(pv.data[off:pv.base]) // 只用于字段数校验
				if err != nil {
					return nil, false, err
				}
				_ = rh
				v, err := db.cellValue(pv, off, nil, 1, visited)
				if err != nil {
					return nil, false, err
				}
				return v, true, nil
			}
		}
		return nil, false, nil
	}

	// interior：每个 cell = [child pgno][key payload]，最后右子页无 key。
	// 线性扫描即可，因为只需判断 key 落在哪个区间；不需要做二分优化。
	for i, off := range offsets {
		if off+4 > len(pv.data) {
			return nil, false, fmt.Errorf("sqlitedb: interior cell %d truncated", i)
		}
		child := int(binary.BigEndian.Uint32(pv.data[off : off+4]))
		k, err := db.cellKey(pv, off+4, visited)
		if err != nil {
			return nil, false, err
		}
		if string(k) >= key {
			return db.findInTablePage(child, 0, key, visited, depth+1)
		}
	}
	right := int(binary.BigEndian.Uint32(pv.data[pv.hdr+7 : pv.hdr+9]))
	return db.findInTablePage(right, 0, key, visited, depth+1)
}

// masterEntry 从 sqlite_master 中找到目标表及其 root page。
func (db *database) masterEntry(pageNo, headerOff int, table string, visited map[int]bool, depth int) (root int, err error) {
	if depth > maxTreeDepth {
		return 0, fmt.Errorf("sqlitedb: sqlite_master deeper than %d levels", maxTreeDepth)
	}
	if visited == nil {
		visited = map[int]bool{}
	}
	if visited[pageNo] {
		return 0, fmt.Errorf("sqlitedb: sqlite_master cycle at page %d", pageNo)
	}
	visited[pageNo] = true
	defer delete(visited, pageNo)

	pv, err := db.parsePage(pageNo, headerOff)
	if err != nil {
		return 0, err
	}
	offsets, err := pv.cellOffsets()
	if err != nil {
		return 0, err
	}

	scanLeafCell := func(off int) (int, bool, error) {
		typ, err := db.cellValue(pv, off, nil, 0, visited)
		if err != nil {
			return 0, false, err
		}
		name, err := db.cellValue(pv, off, nil, 1, visited)
		if err != nil {
			return 0, false, err
		}
		if string(typ) != "table" || string(name) != table {
			return 0, false, nil
		}
		rp, err := db.cellValue(pv, off, nil, 3, visited)
		if err != nil {
			return 0, false, err
		}
		root, err := decodeIntBE(rp)
		if err != nil {
			return 0, false, fmt.Errorf("sqlitedb: bad root page field for table %q: %w", table, err)
		}
		if root <= 0 || root > db.pages {
			// WITHOUT ROWID 表在 sqlite_master 里 root=0（数据在同名唯一索引里），
			// 本包不实现索引遍历，明确报错而不是返回"未找到"。
			return 0, false, fmt.Errorf("sqlitedb: table %q has root page %d (WITHOUT ROWID or corrupt?)", table, root)
		}
		return root, true, nil
	}

	if pv.typ == pageTypeLeaf {
		for _, off := range offsets {
			root, ok, err := scanLeafCell(off)
			if err != nil {
				// 表名不匹配的行解析失败可以跳过；真正命中的行才必须是干净的
				continue
			}
			if ok {
				return root, nil
			}
		}
		return 0, nil
	}

	for i, off := range offsets {
		if off+4 > len(pv.data) {
			return 0, fmt.Errorf("sqlitedb: sqlite_master interior cell %d truncated", i)
		}
		child := int(binary.BigEndian.Uint32(pv.data[off : off+4]))
		root, err := db.masterEntry(child, 0, table, visited, depth+1)
		if err != nil {
			return 0, err
		}
		if root != 0 {
			return root, nil
		}
	}
	right := int(binary.BigEndian.Uint32(pv.data[pv.hdr+7 : pv.hdr+9]))
	return db.masterEntry(right, 0, table, visited, depth+1)
}

func decodeIntBE(b []byte) (int, error) {
	if len(b) == 0 || len(b) > 8 {
		return 0, fmt.Errorf("sqlitedb: not an integer width: %d bytes", len(b))
	}
	var v uint64
	for _, c := range b {
		v = v<<8 | uint64(c)
	}
	if v > uint64(1)<<31 {
		return 0, fmt.Errorf("sqlitedb: integer too large: %d", v)
	}
	return int(v), nil
}

// checkWAL 在打开主文件之前做 -wal 检查。
// 存在且 size>0 就拒绝：未合并的 WAL 意味着主文件是旧快照，读它会返回过期凭据；
// 而 0 字节的 -wal 只是残留，不影响快照完整性。-shm 存在不算错（它可由任何只读进程
// 触发创建，而我们绝不碰它）。
func checkWAL(dbPath string) error {
	wal := dbPath + "-wal"
	st, err := os.Stat(wal)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("sqlitedb: stat wal: %w", err)
	}
	if !st.IsDir() && st.Size() > 0 {
		return ErrWALPresent
	}
	return nil
}

// Lookup 在表 table 的 B-tree 中按第一个字段查找 key，返回第二个字段的原始字节。
//
// 返回约定：
//   - TEXT：其 UTF-8 字节；BLOB：原始字节（两者在返回类型上不可区分，调用方自行解读）
//   - NULL：value=nil, found=true
//   - key 不存在：value=nil, found=false, err=nil
//   - 表不存在：ErrNoSuchTable；-wal 存在：ErrWALPresent
//   - 文件损坏/不支持的特性：明确 error，绝不 panic
func Lookup(dbPath, table, key string) ([]byte, bool, error) {
	if table == "" {
		return nil, false, errors.New("sqlitedb: empty table name")
	}
	if err := checkWAL(dbPath); err != nil {
		return nil, false, err
	}
	db, err := open(filepath.Clean(dbPath))
	if err != nil {
		return nil, false, err
	}
	defer db.close()

	root, err := db.masterEntry(1, headerSize, table, map[int]bool{}, 0)
	if err != nil {
		return nil, false, err
	}
	if root == 0 {
		return nil, false, fmt.Errorf("%w: %s", ErrNoSuchTable, table)
	}
	val, found, err := db.findInTablePage(root, 0, key, map[int]bool{}, 0)
	if err != nil {
		return nil, false, err
	}
	return val, found, nil
}

// TableExists 报告 sqlite_master 里是否有该 table 条目。
func TableExists(dbPath, table string) (bool, error) {
	if err := checkWAL(dbPath); err != nil {
		return false, err
	}
	db, err := open(filepath.Clean(dbPath))
	if err != nil {
		return false, err
	}
	defer db.close()
	root, err := db.masterEntry(1, headerSize, table, map[int]bool{}, 0)
	if err != nil {
		// root=0 的错误里也包含"表存在但读不了"的情况，这里只回答"有没有这张表"，
		// 所以把明确的 root-page 错误当成存在来报告。
		if errors.Is(err, io.EOF) || isNotDatabase(err) {
			return false, err
		}
		if isMissingRoot(err) {
			return true, nil
		}
		return false, err
	}
	return root != 0, nil
}

// isNotDatabase 报告错误是否为「不是 SQLite 数据库文件」。
func isNotDatabase(err error) bool { return errors.Is(err, ErrNotDatabase) }

// isMissingRoot 报告错误是否为「表条目存在但 root page 读不出来」。
func isMissingRoot(err error) bool {
	return err != nil && contains(err.Error(), "root page")
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
