//
//  TennisContract.swift — 三端共享契约（Watch / Phone / Server）
//
//  ⚠️⚠️ 本文件是**唯一真源**。App 工程里的副本由脚本同步生成，不要直接改副本。
//
//      改契约：  先改本文件 → bash scripts/sync_contract.sh
//      查漂移：  bash scripts/sync_contract.sh --check
//      验一致：  .venv/bin/python scripts/verify_contract.py
//
//  契约文档： docs/CONTRACT.md（字段表）· docs/OVERVIEW.md（术语与枚举）
//  契约版本： v1.0.0      （改动本文件必须同时递增版本号并记入 CONTRACT.md §8）
//
//  ---------------------------------------------------------------------------
//  为什么要有这个文件
//  ---------------------------------------------------------------------------
//  之前三端各写各的：Watch 的采集模型直接当传输格式用，结果 6 处字段名与后端
//  入参对不上。这类不一致**不会报错** —— 接口返回 200、库里也有行，只是关键
//  指标全是 NULL，属于最难发现的一类故障。
//
//  本文件把「线上跑的数据长什么样」变成**可编译的类型**：
//    · 字段名写错 → 编译不过，而不是静默丢数据
//    · 日期编码错误 → 在本文件的 Codable 实现里一次性挡住
//    · 枚举少一个值 → 与后端 STROKE_TYPES 的差异由 verify_contract.py 抓出来
//
//  ---------------------------------------------------------------------------
//  关于 nonisolated
//  ---------------------------------------------------------------------------
//  Phone 工程开了 `SWIFT_DEFAULT_ACTOR_ISOLATION = MainActor`，Watch 没开。
//  同一份文件在两个 target 下默认隔离域不同，会造成"在 Watch 能编译、在 Phone
//  编译不过"的假故障。契约类型一律显式 nonisolated，两端行为严格一致。
//

import Foundation

// MARK: - 契约常量

/// 三端共用的字面量。**任何一端都不许再自己写死这些值。**
nonisolated public enum TennisContract {

    /// 契约版本。与 `docs/CONTRACT.md` §8 变更记录同步递增。
    public static let version = "1.0.0"

    // ---- 接口路径 ----
    public static let sessionsPath         = "/api/prod/sessions"
    public static let rawSessionsPath      = "/api/raw/sessions"
    public static let rawSessionsJSONPath  = "/api/raw/sessions/json"
    public static let wechatLoginPath      = "/api/wechat/login"
    public static let wechatLogoutPath     = "/api/wechat/logout"
    public static let mePath               = "/api/prod/me"
    public static let profilePath          = "/api/prod/profile"
    public static let analysisPath         = "/api/prod/analysis"
    public static let leaderboardPath      = "/api/prod/leaderboard"

    // ---- 请求头 ----
    public enum Header {
        /// 写口鉴权。与后端环境变量 `NETPULSE_INGEST_KEY` 同值；未配置时后端放行。
        public static let ingestKey     = "X-Ingest-Key"
        /// 设备标识，仅审计用（排查"哪台设备上报的"）。
        public static let deviceId      = "X-Device-Id"
        /// 读口鉴权，`Bearer <token>`，由 `/api/wechat/login` 签发。
        public static let authorization = "Authorization"
        public static let contentType   = "Content-Type"
    }

    // ---- 建议运行参数 ----
    public enum Limits {
        public static let sessionUploadTimeout: TimeInterval = 30
        public static let rawUploadTimeout: TimeInterval = 120
        /// 逐拍批大小（服务端单批上限 500，留余量）。
        public static let strokeBatchSize = 200
        public static let serverBatchLimit = 500
        /// 相邻两拍间隔小于该值视为同一回合。**必须与后端 `RALLY_GAP_SEC` 一致**
        /// （`server/ingest.py`），否则最长回合数会算错。
        public static let rallyGapSeconds: Double = 2.5
    }

    /// 契约里出现过的全部 entity_type / action，用于跨端对账。
    public enum Sync {
        public static let entityTypes = [
            "student_profile", "training_session", "stroke_record", "feedback_ticket",
        ]
        public static let actions = ["create", "update", "delete"]
    }

    // ---- 生物力学口径 ----
    public enum Biomechanics {
        /// 拍头线速度的等效半径（米）：
        ///     速度(km/h) = 峰值角速度(rad/s) × r × 3.6
        ///
        /// ⚠️ **这个口径待拍板，别自行改**：
        ///   · `docs/OVERVIEW.md` §3.2 与后端 `ingest.py` 的注释都写 **0.685**（球拍长度）；
        ///   · Watch 采集端长期实现用的是 **1.05**（拍臂等效半径：手臂 + 球拍）。
        ///   两者差 35%，会让**全部已展示的球速数字发生变化** ——
        ///   这是产品口径问题，不是代码问题，所以不在统一改造里擅自改。
        ///   在拍板前，三端一律用本常量（= 现有实现值，保证线上数字不变）。
        ///   已登记为 [ROADMAP.md] 待决项。
        public static let racketRadiusMeters: Double = 1.05
    }
}

// MARK: - 时间口径

/// 线上时间一律 ISO8601 字符串（UTC，形如 `2026-10-01T02:26:47Z`）。
///
/// ⚠️ **绝不允许**发 Apple 参考纪元或 Unix 纪元数字：两者相差 978307200 秒
/// ≈ 31 年，服务端无从猜测是哪一个。发字符串是唯一没有歧义的做法。
nonisolated public enum WireDate {

    /// 上行（我们产生）：固定无毫秒，与 `docs/CONTRACT.md` §3.1 示例一致。
    public static func string(from date: Date) -> String {
        date.formatted(.iso8601)
    }

    /// 下行（我们解析）：带毫秒与不带毫秒都接受
    /// （后端 `db.normalize_ts` 两侧都可能产出）。
    public static func date(from string: String) -> Date? {
        if let d = try? Date(string, strategy: .iso8601) { return d }
        return try? Date(
            string,
            strategy: .iso8601
                .year().month().day()
                .dateSeparator(.dash)
                .time(includingFractionalSeconds: true)
                .timeSeparator(.colon)
        )
    }
}

// MARK: - 挥拍类型

/// 挥拍类型。取值与后端 `server/ingest.py: STROKE_TYPES` **逐字一致**。
///
/// 6 类之外多一个 `unknown`：**检测到但没分类成功**的拍。
/// 后端对它的处理是 —— 计入 `stroke_count`，但不写入 `stroke_records` 明细，
/// 也不进六类分项计数。因此「六类之和 ≤ stroke_count」是正常的，差额就是未识别拍。
nonisolated public enum SwingType: String, Codable, CaseIterable, Sendable {
    case forehand = "forehand"
    case backhand = "backhand"
    case serve    = "serve"
    case slice    = "slice"
    case volley   = "volley"
    case smash    = "smash"
    case unknown  = "unknown"

    /// **六类**，不含 `unknown`。配平校验与 UI 构成图一律用这个，
    /// 不要用 `allCases`（那样会把未识别拍算进分母）。
    public static var classifiedCases: [SwingType] {
        [.forehand, .backhand, .serve, .slice, .volley, .smash]
    }

    /// 是否已成功分类。未分类的拍不入 `stroke_records`。
    public var isClassified: Bool { self != .unknown }

    public var displayName: String {
        switch self {
        case .forehand: return "正手"
        case .backhand: return "反手"
        case .serve:    return "发球"
        case .slice:    return "切削"
        case .volley:   return "截击"
        case .smash:    return "高压"
        case .unknown:  return "未识别"
        }
    }
}

// MARK: - 会话形式

/// 训练形式。后端缺失时会按击球构成自动推断，客户端有用户手选值应优先传。
nonisolated public enum SessionType: String, Codable, CaseIterable, Sendable {
    case drill = "drill"   // 专项练习
    case rally = "rally"   // 对拉
    case match = "match"   // 实战对抗
    case serve = "serve"   // 发球训练
}

/// 佩戴手腕。决定正反手分类的符号方向，缺失会导致正反手整体互换。
nonisolated public enum Wrist: String, Codable, CaseIterable, Sendable {
    case left  = "left"
    case right = "right"
}

// MARK: - 上行 DTO

/// 一次挥拍（上行形态）。
///
/// ⚠️ 采集端内部字段（`peakRotation` / `peakAccel` / `estimatedSpeed` /
/// `timestamp`）**不属于本契约**。要么换算后填进下面这几个字段，
/// 要么走 L0 原始层，不要塞进来 —— 契约外的字段会被后端白名单**静默丢弃**。
nonisolated public struct MatchSwing: Codable, Sendable, Equatable {
    /// 击球类型。`unknown` 会被后端跳过明细但仍计入总数。
    public var type: SwingType
    /// 相对**会话开始**的秒数。
    /// ⚠️ 不是绝对时间戳、不是字符串。这个字段决定 `impact_ms` 与 `rally_max`，
    /// 给错类型不会报错，只会让「最长回合」变成 NULL。
    public var impactTime: Double?
    /// 拍头线速度（km/h）。产品文案叫「球速」，物理量实际是拍头线速度。
    public var racketHeadSpeedKmh: Double?
    /// 分类置信度 0~1。缺失会让质量门禁 G3 失效。
    public var confidence: Double?

    public init(type: SwingType,
                impactTime: Double? = nil,
                racketHeadSpeedKmh: Double? = nil,
                confidence: Double? = nil) {
        self.type = type
        self.impactTime = impactTime
        self.racketHeadSpeedKmh = racketHeadSpeedKmh
        self.confidence = confidence
    }
}

/// 一场训练的**上行唯一数据形态**。Watch 产出 → Phone 缓存并转发 → Server 消费。
///
/// 字段名必须与 `docs/CONTRACT.md` §3 逐字一致。日期字段在本类型的 Codable
/// 实现里**强制转成 ISO8601 字符串**，因此无论调用方配了什么 `JSONEncoder`，
/// 都不会退化成数字时间戳。
nonisolated public struct MatchSession: Codable, Sendable {

    // ---- 身份与时间 ----
    /// 跨端幂等键，客户端生成（UUID 字符串）。缺失 → 后端 400 拒绝。
    public var id: String
    public var startedAt: Date
    public var endedAt: Date?
    /// 会话时长（秒）。缺失时后端用 `endedAt − startedAt` 复算。
    public var duration: Int?

    // ---- 采集侧元数据 ----
    public var wrist: Wrist?
    public var title: String?
    public var sessionType: SessionType?
    public var location: String?
    public var courtType: String?

    // ---- 会话级生理与负荷（**标量**，不是数组） ----
    /// 平均心率。注意不是 `heartRates[]` —— 采样数组走 L0 原始层。
    public var avgHeartRate: Int?
    public var maxHeartRate: Int?
    public var activeCalories: Double?
    public var distanceKm: Double?

    // ---- 逐拍 ----
    public var swings: [MatchSwing]

    public init(id: String,
                startedAt: Date,
                endedAt: Date? = nil,
                duration: Int? = nil,
                wrist: Wrist? = nil,
                title: String? = nil,
                sessionType: SessionType? = nil,
                location: String? = nil,
                courtType: String? = nil,
                avgHeartRate: Int? = nil,
                maxHeartRate: Int? = nil,
                activeCalories: Double? = nil,
                distanceKm: Double? = nil,
                swings: [MatchSwing] = []) {
        self.id = id
        self.startedAt = startedAt
        self.endedAt = endedAt
        self.duration = duration
        self.wrist = wrist
        self.title = title
        self.sessionType = sessionType
        self.location = location
        self.courtType = courtType
        self.avgHeartRate = avgHeartRate
        self.maxHeartRate = maxHeartRate
        self.activeCalories = activeCalories
        self.distanceKm = distanceKm
        self.swings = swings
    }

    // MARK: Codable —— 手写而非合成

    private enum CodingKeys: String, CodingKey {
        case id, startedAt, endedAt, duration
        case wrist, title, sessionType, location, courtType
        case avgHeartRate, maxHeartRate, activeCalories, distanceKm
        case swings
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        startedAt = Self.decodeDate(c, .startedAt) ?? Date()
        endedAt = Self.decodeDate(c, .endedAt)
        duration = try c.decodeIfPresent(Int.self, forKey: .duration)
        wrist = try c.decodeIfPresent(Wrist.self, forKey: .wrist)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        sessionType = try c.decodeIfPresent(SessionType.self, forKey: .sessionType)
        location = try c.decodeIfPresent(String.self, forKey: .location)
        courtType = try c.decodeIfPresent(String.self, forKey: .courtType)
        avgHeartRate = try c.decodeIfPresent(Int.self, forKey: .avgHeartRate)
        maxHeartRate = try c.decodeIfPresent(Int.self, forKey: .maxHeartRate)
        activeCalories = try c.decodeIfPresent(Double.self, forKey: .activeCalories)
        distanceKm = try c.decodeIfPresent(Double.self, forKey: .distanceKm)
        swings = try c.decodeIfPresent([MatchSwing].self, forKey: .swings) ?? []
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(WireDate.string(from: startedAt), forKey: .startedAt)
        if let endedAt { try c.encode(WireDate.string(from: endedAt), forKey: .endedAt) }
        try c.encodeIfPresent(duration, forKey: .duration)
        try c.encodeIfPresent(wrist, forKey: .wrist)
        try c.encodeIfPresent(title, forKey: .title)
        try c.encodeIfPresent(sessionType, forKey: .sessionType)
        try c.encodeIfPresent(location, forKey: .location)
        try c.encodeIfPresent(courtType, forKey: .courtType)
        try c.encodeIfPresent(avgHeartRate, forKey: .avgHeartRate)
        try c.encodeIfPresent(maxHeartRate, forKey: .maxHeartRate)
        try c.encodeIfPresent(activeCalories, forKey: .activeCalories)
        try c.encodeIfPresent(distanceKm, forKey: .distanceKm)
        try c.encode(swings, forKey: .swings)
    }

    /// 容忍两种输入：ISO8601 字符串，或（历史数据里的）数字时间戳 —— 后者按
    /// Unix 秒而非 Apple 参考纪元解释，且只用于读，不用于写。
    private static func decodeDate(
        _ c: KeyedDecodingContainer<CodingKeys>,
        _ key: CodingKeys
    ) -> Date? {
        if let s = try? c.decodeIfPresent(String.self, forKey: key) {
            return WireDate.date(from: s)
        }
        if let n = try? c.decodeIfPresent(Double.self, forKey: key) {
            return Date(timeIntervalSince1970: n)
        }
        return nil
    }
}

// MARK: - 上行请求体

/// `POST /api/prod/sessions` 的请求体。
nonisolated public struct SessionUploadRequest: Codable, Sendable {
    /// 身份根。`studentId` 为空时后端据此自动开通学员档案。
    public var openid: String
    /// 显式指定归属学员；缺省用 `stu-wx-<sha1(openid)[:12]>`。
    public var studentId: String?
    /// 首次开通学员档案时写入的名字。
    public var studentName: String?
    /// 关掉可只传场次聚合，流量极小。
    public var includeStrokes: Bool
    public var session: MatchSession

    public init(openid: String,
                session: MatchSession,
                studentId: String? = nil,
                studentName: String? = nil,
                includeStrokes: Bool = true) {
        self.openid = openid
        self.session = session
        self.studentId = studentId
        self.studentName = studentName
        self.includeStrokes = includeStrokes
    }

    private enum CodingKeys: String, CodingKey {
        case openid
        case studentId = "student_id"
        case studentName = "student_name"
        case includeStrokes = "include_strokes"
        case session
    }
}

/// `POST /api/prod/sessions` 的响应。
/// `rawLayer.status` 为 `created` / `duplicate` **都算成功**。
nonisolated public struct SessionUploadResponse: Codable, Sendable {

    nonisolated public struct RawLayer: Codable, Sendable {
        public var status: String
        public var rawId: String?
        public var revision: Int?
        public var sha256: String?
        public var byteSize: Int?

        private enum CodingKeys: String, CodingKey {
            case status
            case rawId = "raw_id"
            case revision
            case sha256
            case byteSize = "byte_size"
        }

        /// `created` 与 `duplicate` 都是成功 —— 上行是幂等的，重复上传不算失败。
        public var isSuccess: Bool { status == "created" || status == "duplicate" }
    }

    nonisolated public struct AnalysisDB: Codable, Sendable {
        public var applied: Int
        public var duplicate: Int
    }

    public var ok: Bool
    public var sessionId: String?
    public var externalId: String?
    public var strokesIngested: Int?
    public var analysisDb: AnalysisDB?
    public var rawLayer: RawLayer?
    public var studentId: String?
    public var studentProvisioned: Bool?

    private enum CodingKeys: String, CodingKey {
        case ok
        case sessionId = "session_id"
        case externalId = "external_id"
        case strokesIngested = "strokes_ingested"
        case analysisDb = "analysis_db"
        case rawLayer = "raw_layer"
        case studentId = "student_id"
        case studentProvisioned = "student_provisioned"
    }

    /// 幂等命中。**不要当失败重试。**
    public var isDuplicate: Bool { (analysisDb?.duplicate ?? 0) > 0 }
}

// MARK: - 下行 DTO

/// 六类击球计数（`GET /api/prod/sessions` 响应的 `counts` 字段）。
///
/// 属性名必须与 `SwingType.classifiedCases` 逐字一致 ——
/// 后端是按 `'%s_count' % type` 拼列名的，改名会静默变成 null。
nonisolated public struct StrokeCounts: Codable, Sendable {
    public var forehand: Int?
    public var backhand: Int?
    public var serve: Int?
    public var slice: Int?
    public var volley: Int?
    public var smash: Int?

    public init(forehand: Int? = nil, backhand: Int? = nil, serve: Int? = nil,
                slice: Int? = nil, volley: Int? = nil, smash: Int? = nil) {
        self.forehand = forehand
        self.backhand = backhand
        self.serve = serve
        self.slice = slice
        self.volley = volley
        self.smash = smash
    }

    /// 按类型取计数。`unknown` 在响应里没有对应字段（后端不单独统计未识别拍），
    /// 用 `strokeCount - classifiedTotal` 反推。
    public subscript(type: SwingType) -> Int {
        switch type {
        case .forehand: return forehand ?? 0
        case .backhand: return backhand ?? 0
        case .serve:    return serve ?? 0
        case .slice:    return slice ?? 0
        case .volley:   return volley ?? 0
        case .smash:    return smash ?? 0
        case .unknown:  return 0
        }
    }

    /// 六类之和。与 `strokeCount` 的差额 = 未识别拍数（后端 G2 配平门禁同一口径）。
    public var classifiedTotal: Int {
        SwingType.classifiedCases.reduce(0) { $0 + self[$1] }
    }
}

/// `GET /api/prod/sessions` 的单条会话。
///
/// 时间字段保持 **String**（`2026-09-28T09:00:00.000Z`，后端 `normalize_ts` 的产物，
/// **带毫秒**）。不直接声明为 `Date` 是因为 `.iso8601` 解码策略不认毫秒，
/// 会整包解码失败 —— 用 `startedAtDate` 走 `WireDate` 转换，两种格式都吃。
nonisolated public struct SessionListItem: Codable, Sendable {
    /// L2 主键（`nps-<externalId>`）。**不是**采集端 id。
    public var id: String
    /// 采集端会话 id —— 与 `MatchSession.id` 同一个值，跨端对账用它。
    public var externalId: String?
    public var title: String?
    public var sessionType: String?
    public var location: String?
    public var startedAt: String?
    public var endedAt: String?
    public var duration: Int?
    /// 后端已格式化好的中文时长，如 `30 分 0 秒`。UI 直接用，不要自己再格式化一遍。
    public var durationLabel: String?
    public var wrist: String?
    public var counts: StrokeCounts?
    /// 检测到的总拍数（含未识别拍）。
    public var total: Int?
    public var avgHeartRate: Int?
    public var maxHeartRate: Int?
    public var calories: Double?
    public var distanceKm: Double?
    public var avgSpeedKmh: Double?
    public var peakSpeedKmh: Double?
    public var servePeakKmh: Double?
    public var rallyMax: Int?
    /// 后端**没有数据源**（手腕单点 IMU 测不到甜区），恒为 null。
    /// UI 不要把这个字段当真实数据展示。
    public var sweetSpotRate: Double?

    public var startedAtDate: Date? { startedAt.flatMap(WireDate.date(from:)) }
    public var endedAtDate: Date? { endedAt.flatMap(WireDate.date(from:)) }

    /// 未识别拍数 = 总拍数 − 六类之和。
    public var unidentifiedCount: Int? {
        guard let total else { return nil }
        return max(0, total - (counts?.classifiedTotal ?? 0))
    }
}

/// `GET /api/prod/sessions` 的响应。
nonisolated public struct SessionListResponse: Codable, Sendable {
    public var count: Int
    public var range: String?
    public var studentId: String?
    public var sessions: [SessionListItem]
}

// MARK: - WCSession 消息契约

/// Watch → Phone。
///
/// 只有 `sessionEnded` 携带完整 `MatchSession`；实时阶段只发**计数与心率**，
/// 不要每拍发一次全量数组。载荷一律 JSON 编码后放在字典的 `"message"` 键下。
nonisolated public enum WatchToPhoneMessage: Codable, Sendable {
    case sessionStarted(SessionStartedPayload)
    case swingUpdate(SwingUpdatePayload)
    case sessionEnded(SessionEndedPayload)

    nonisolated public struct SessionStartedPayload: Codable, Sendable {
        public var sessionId: String
        public var startedAt: Date
        public var wrist: Wrist
        public init(sessionId: String, startedAt: Date, wrist: Wrist) {
            self.sessionId = sessionId
            self.startedAt = startedAt
            self.wrist = wrist
        }
        private enum CodingKeys: String, CodingKey { case sessionId, startedAt, wrist }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            sessionId = try c.decode(String.self, forKey: .sessionId)
            startedAt = WireDate.date(from: (try? c.decode(String.self, forKey: .startedAt)) ?? "")
                ?? Date()
            wrist = try c.decode(Wrist.self, forKey: .wrist)
        }
        public func encode(to encoder: Encoder) throws {
            var c = encoder.container(keyedBy: CodingKeys.self)
            try c.encode(sessionId, forKey: .sessionId)
            try c.encode(WireDate.string(from: startedAt), forKey: .startedAt)
            try c.encode(wrist, forKey: .wrist)
        }
    }

    nonisolated public struct SwingUpdatePayload: Codable, Sendable {
        public var sessionId: String
        public var totalSwings: Int
        public var lastType: SwingType
        public var lastConfidence: Double?
        public var heartRate: Int?
        public init(sessionId: String, totalSwings: Int, lastType: SwingType,
                    lastConfidence: Double? = nil, heartRate: Int? = nil) {
            self.sessionId = sessionId
            self.totalSwings = totalSwings
            self.lastType = lastType
            self.lastConfidence = lastConfidence
            self.heartRate = heartRate
        }
    }

    nonisolated public struct SessionEndedPayload: Codable, Sendable {
        public var session: MatchSession
        public init(session: MatchSession) { self.session = session }
    }

    // ---- 判别式编码：{"kind": "...", "payload": {...}} ----

    private enum Kind: String, Codable {
        case sessionStarted, swingUpdate, sessionEnded
    }

    private enum CodingKeys: String, CodingKey { case kind, payload }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(Kind.self, forKey: .kind) {
        case .sessionStarted:
            self = .sessionStarted(try c.decode(SessionStartedPayload.self, forKey: .payload))
        case .swingUpdate:
            self = .swingUpdate(try c.decode(SwingUpdatePayload.self, forKey: .payload))
        case .sessionEnded:
            self = .sessionEnded(try c.decode(SessionEndedPayload.self, forKey: .payload))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .sessionStarted(let p):
            try c.encode(Kind.sessionStarted, forKey: .kind)
            try c.encode(p, forKey: .payload)
        case .swingUpdate(let p):
            try c.encode(Kind.swingUpdate, forKey: .kind)
            try c.encode(p, forKey: .payload)
        case .sessionEnded(let p):
            try c.encode(Kind.sessionEnded, forKey: .kind)
            try c.encode(p, forKey: .payload)
        }
    }
}

/// Phone → Watch。
nonisolated public enum PhoneToWatchMessage: Codable, Sendable {
    case setWrist(SetWristPayload)
    case requestTrainingData(RequestTrainingDataPayload)

    nonisolated public struct SetWristPayload: Codable, Sendable {
        public var wrist: Wrist
        public init(wrist: Wrist) { self.wrist = wrist }
    }

    nonisolated public struct RequestTrainingDataPayload: Codable, Sendable {
        public var sessionId: String
        public init(sessionId: String) { self.sessionId = sessionId }
    }

    private enum Kind: String, Codable {
        case setWrist, requestTrainingData
    }

    private enum CodingKeys: String, CodingKey { case kind, payload }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(Kind.self, forKey: .kind) {
        case .setWrist:
            self = .setWrist(try c.decode(SetWristPayload.self, forKey: .payload))
        case .requestTrainingData:
            self = .requestTrainingData(
                try c.decode(RequestTrainingDataPayload.self, forKey: .payload))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .setWrist(let p):
            try c.encode(Kind.setWrist, forKey: .kind)
            try c.encode(p, forKey: .payload)
        case .requestTrainingData(let p):
            try c.encode(Kind.requestTrainingData, forKey: .kind)
            try c.encode(p, forKey: .payload)
        }
    }
}

// MARK: - 便捷编解码

nonisolated public extension TennisContract {
    /// 契约对象专用编码器。`MatchSession` 自己已强制 ISO8601，这里再设一次是
    /// 为了覆盖**其它**可能被一起编码的类型，防止误用默认策略。
    static func makeEncoder() -> JSONEncoder {
        let e = JSONEncoder()
        e.dateEncodingStrategy = .iso8601
        e.outputFormatting = [.withoutEscapingSlashes]
        return e
    }

    static func makeDecoder() -> JSONDecoder {
        let d = JSONDecoder()
        d.dateDecodingStrategy = .iso8601
        return d
    }

    /// WCSession 字典载荷的键名。**统一在这里**，不要在两端各写一遍字面量。
    static let wcMessageKey = "message"
}
