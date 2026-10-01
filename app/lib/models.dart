// 图书、章节、卡片、试题及审计结果的数据结构。
// 提供字段映射、序列化及派生属性。
library;

import 'dart:convert';

class Book {
  final int id;
  final String title;
  final String filename;
  final int totalPages;
  final bool scanned;
  // 扫描版是否带可用的 OCR 文字层。文字版恒为 true;
  // 扫描版带层的可以手动改走"文字制卡"备用路径(质量一般不如视觉)
  final bool hasTextLayer;
  final String createdAt;

  Book.fromMap(Map<String, Object?> m)
    : id = m['id'] as int,
      title = m['title'] as String,
      filename = m['filename'] as String,
      totalPages = m['total_pages'] as int,
      scanned = (m['scanned'] as int) == 1,
      hasTextLayer = (m['text_layer'] as int? ?? 0) == 1,
      createdAt = m['created_at'] as String;
}

class Chapter {
  final int id;
  final int bookId;
  final int idx;
  final String title;
  final int pageStart;
  final int pageEnd;
  final int charCount;

  Chapter.fromMap(Map<String, Object?> m)
    : id = m['id'] as int,
      bookId = m['book_id'] as int,
      idx = m['idx'] as int,
      title = m['title'] as String,
      pageStart = m['page_start'] as int,
      pageEnd = m['page_end'] as int,
      charCount = m['char_count'] as int;
}

// 卡片的三种状态,AI生成的卡先pending等人工校对,通过了才active进背诵队列
class CardStatus {
  static const pending = 'pending';
  static const active = 'active';
  static const suspended = 'suspended';
}

// FSRS里卡片的学习阶段,0-3
class CardState {
  static const newCard = 0;
  static const learning = 1;
  static const review = 2;
  static const relearning = 3;
}

// 研究版审计状态。evidenceAnchored 只表示引文逐字存在,不等于答案已经正确。
class AuditStatus {
  static const notAudited = 'not_audited';
  static const evidenceAnchored = 'evidence_anchored';
  static const lowRisk = 'low_risk';
  static const needsReview = 'needs_review';
  static const passed = 'passed';
  static const repair = 'repair';
  static const rejected = 'rejected';
  static const abstained = 'abstained';
  static const stale = 'stale';

  static String label(String value) => switch (value) {
    evidenceAnchored => '证据已定位',
    lowRisk => '本地审计：低风险',
    needsReview => '本地审计：需复核',
    passed => '核验通过',
    repair => '建议修复',
    rejected => '核验拒绝',
    abstained => '模型拒判',
    stale => '编辑后待复核',
    _ => '尚未核验',
  };

  // Only an explicit structured PASS is eligible for standard release.
  // Local span scores and mere evidence anchoring are diagnostic states; the
  // separate force-approve path is the only intentional human override.
  static bool allowsStandardRelease(String value) => value == passed;

  static bool blocksStandardRelease(String value) =>
      !allowsStandardRelease(value);
}

// Canonical fields persisted with a hosted audit. The database release gate
// revalidates this contract instead of trusting audit_status on its own.
class CardAuditContract {
  static const schemaVersion = 'rehearsalguard.card_audit.v2';
  static const supported = 'SUPPORTED';
  static const contradicted = 'CONTRADICTED';
  static const notEnoughInfo = 'NOT_ENOUGH_INFO';
  static const pass = 'PASS';
  static const repair = 'REPAIR';
  static const reject = 'REJECT';
}

// 独立本地审计器在卡片答案中返回的可疑字符区间。
// start/end 是 Dart String 的 UTF-16 code-unit offset，所以可直接 substring。
class AuditSpan {
  final int start;
  final int end;
  final String text;
  final double riskScore;

  const AuditSpan({
    required this.start,
    required this.end,
    required this.text,
    required this.riskScore,
  });

  factory AuditSpan.fromMap(Map<String, Object?> value) {
    final start = value['start'];
    final end = value['end'];
    final text = value['text'];
    final score = value['risk_score'];
    if (start is! int || end is! int || text is! String || score is! num) {
      throw const FormatException('审计跨度字段类型错误');
    }
    if (start < 0 || end <= start || score.isNaN || score < 0 || score > 1) {
      throw const FormatException('审计跨度数值非法');
    }
    return AuditSpan(
      start: start,
      end: end,
      text: text,
      riskScore: score.toDouble(),
    );
  }

  Map<String, Object?> toMap() => {
    'start': start,
    'end': end,
    'text': text,
    'risk_score': riskScore,
  };

  static List<AuditSpan> decodeAndValidate(String raw, String answer) {
    final decoded = jsonDecode(raw);
    if (decoded is! List) throw const FormatException('审计跨度不是数组');
    final spans = [
      for (final value in decoded)
        if (value is Map)
          AuditSpan.fromMap(value.cast<String, Object?>())
        else
          throw const FormatException('审计跨度不是对象'),
    ];
    var previousEnd = 0;
    for (final span in spans) {
      if (span.start < previousEnd || span.end > answer.length) {
        throw const FormatException('审计跨度越界或重叠');
      }
      if (answer.substring(span.start, span.end) != span.text) {
        throw const FormatException('审计跨度文本与答案不匹配');
      }
      previousEnd = span.end;
    }
    return spans;
  }
}

class CardItem {
  int id;
  int bookId;
  int? chapterId;
  String type; // 'qa' | 'cloze'
  String front;
  String back;
  int? sourcePage;
  String sourceEvidence;
  String sourceContext;
  String sourceEvidenceGroupsJson;
  int? sourceEvidenceStart;
  int? sourceEvidenceEnd;
  String auditStatus;
  double? auditErrorProbability;
  double expectedReviews;
  double? auditPriority;
  String auditQuotesJson;
  String? auditModel;
  String? auditUpdatedAt;
  String? auditReason;
  String auditSpansJson;
  String auditMetadataJson;
  String? auditContentHash;
  int approvalEpoch;
  int? auditApprovalEpoch;
  String? auditSchemaVersion;
  String? auditAnswerLabel;
  String? auditDecision;
  String status;
  double? stability;
  double? difficulty;
  int state;
  int step;
  DateTime? lastReview;
  DateTime due;
  int reps;
  int lapses;
  String? firstReviewedDay; // 本地日期 yyyy-MM-dd,用于每日新卡限额
  String createdAt;

  CardItem({
    required this.id,
    required this.bookId,
    this.chapterId,
    required this.type,
    required this.front,
    required this.back,
    this.sourcePage,
    this.sourceEvidence = '',
    this.sourceContext = '',
    this.sourceEvidenceGroupsJson = '[]',
    this.sourceEvidenceStart,
    this.sourceEvidenceEnd,
    this.auditStatus = AuditStatus.notAudited,
    this.auditErrorProbability,
    this.expectedReviews = 1.0,
    this.auditPriority,
    this.auditQuotesJson = '[]',
    this.auditModel,
    this.auditUpdatedAt,
    this.auditReason,
    this.auditSpansJson = '[]',
    this.auditMetadataJson = '{}',
    this.auditContentHash,
    this.approvalEpoch = 0,
    this.auditApprovalEpoch,
    this.auditSchemaVersion,
    this.auditAnswerLabel,
    this.auditDecision,
    required this.status,
    this.stability,
    this.difficulty,
    this.state = CardState.newCard,
    this.step = 0,
    this.lastReview,
    required this.due,
    this.reps = 0,
    this.lapses = 0,
    this.firstReviewedDay,
    required this.createdAt,
  });

  CardItem copy() => CardItem(
    id: id,
    bookId: bookId,
    chapterId: chapterId,
    type: type,
    front: front,
    back: back,
    sourcePage: sourcePage,
    sourceEvidence: sourceEvidence,
    sourceContext: sourceContext,
    sourceEvidenceGroupsJson: sourceEvidenceGroupsJson,
    sourceEvidenceStart: sourceEvidenceStart,
    sourceEvidenceEnd: sourceEvidenceEnd,
    auditStatus: auditStatus,
    auditErrorProbability: auditErrorProbability,
    expectedReviews: expectedReviews,
    auditPriority: auditPriority,
    auditQuotesJson: auditQuotesJson,
    auditModel: auditModel,
    auditUpdatedAt: auditUpdatedAt,
    auditReason: auditReason,
    auditSpansJson: auditSpansJson,
    auditMetadataJson: auditMetadataJson,
    auditContentHash: auditContentHash,
    approvalEpoch: approvalEpoch,
    auditApprovalEpoch: auditApprovalEpoch,
    auditSchemaVersion: auditSchemaVersion,
    auditAnswerLabel: auditAnswerLabel,
    auditDecision: auditDecision,
    status: status,
    stability: stability,
    difficulty: difficulty,
    state: state,
    step: step,
    lastReview: lastReview,
    due: due,
    reps: reps,
    lapses: lapses,
    firstReviewedDay: firstReviewedDay,
    createdAt: createdAt,
  );

  factory CardItem.fromMap(Map<String, Object?> m) => CardItem(
    id: m['id'] as int,
    bookId: m['book_id'] as int,
    chapterId: m['chapter_id'] as int?,
    type: m['type'] as String,
    front: m['front'] as String,
    back: m['back'] as String,
    sourcePage: m['source_page'] as int?,
    sourceEvidence: (m['source_evidence'] as String?) ?? '',
    sourceContext: (m['source_context'] as String?) ?? '',
    sourceEvidenceGroupsJson: (m['source_evidence_groups'] as String?) ?? '[]',
    sourceEvidenceStart: m['source_evidence_start'] as int?,
    sourceEvidenceEnd: m['source_evidence_end'] as int?,
    auditStatus: (m['audit_status'] as String?) ?? AuditStatus.notAudited,
    auditErrorProbability: (m['audit_error_probability'] as num?)?.toDouble(),
    expectedReviews: (m['audit_expected_reviews'] as num?)?.toDouble() ?? 1.0,
    auditPriority: (m['audit_priority'] as num?)?.toDouble(),
    auditQuotesJson: (m['audit_quotes'] as String?) ?? '[]',
    auditModel: m['audit_model'] as String?,
    auditUpdatedAt: m['audit_updated_at'] as String?,
    auditReason: m['audit_reason'] as String?,
    auditSpansJson: (m['audit_spans'] as String?) ?? '[]',
    auditMetadataJson: (m['audit_metadata'] as String?) ?? '{}',
    auditContentHash: m['audit_content_hash'] as String?,
    approvalEpoch: (m['approval_epoch'] as int?) ?? 0,
    auditApprovalEpoch: m['audit_approval_epoch'] as int?,
    auditSchemaVersion: m['audit_schema_version'] as String?,
    auditAnswerLabel: m['audit_answer_label'] as String?,
    auditDecision: m['audit_decision'] as String?,
    status: m['status'] as String,
    stability: m['stability'] as double?,
    difficulty: m['difficulty'] as double?,
    state: m['state'] as int,
    step: m['step'] as int,
    lastReview: m['last_review'] == null
        ? null
        : DateTime.parse(m['last_review'] as String),
    due: DateTime.parse(m['due'] as String),
    reps: m['reps'] as int,
    lapses: m['lapses'] as int,
    firstReviewedDay: m['first_reviewed_day'] as String?,
    createdAt: m['created_at'] as String,
  );

  Map<String, Object?> toMap() => {
    'id': id,
    'book_id': bookId,
    'chapter_id': chapterId,
    'type': type,
    'front': front,
    'back': back,
    'source_page': sourcePage,
    'source_evidence': sourceEvidence,
    'source_context': sourceContext,
    'source_evidence_groups': sourceEvidenceGroupsJson,
    'source_evidence_start': sourceEvidenceStart,
    'source_evidence_end': sourceEvidenceEnd,
    'audit_status': auditStatus,
    'audit_error_probability': auditErrorProbability,
    'audit_expected_reviews': expectedReviews,
    'audit_priority': auditPriority,
    'audit_quotes': auditQuotesJson,
    'audit_model': auditModel,
    'audit_updated_at': auditUpdatedAt,
    'audit_reason': auditReason,
    'audit_spans': auditSpansJson,
    'audit_metadata': auditMetadataJson,
    'audit_content_hash': auditContentHash,
    'approval_epoch': approvalEpoch,
    'audit_approval_epoch': auditApprovalEpoch,
    'audit_schema_version': auditSchemaVersion,
    'audit_answer_label': auditAnswerLabel,
    'audit_decision': auditDecision,
    'status': status,
    'stability': stability,
    'difficulty': difficulty,
    'state': state,
    'step': step,
    'last_review': lastReview?.toIso8601String(),
    'due': due.toIso8601String(),
    'reps': reps,
    'lapses': lapses,
    'first_reviewed_day': firstReviewedDay,
    'created_at': createdAt,
  };

  List<AuditSpan> get auditSpans {
    try {
      return AuditSpan.decodeAndValidate(auditSpansJson, back);
    } on FormatException {
      return const [];
    }
  }

  List<String> get sourceEvidenceGroups {
    try {
      final decoded = jsonDecode(sourceEvidenceGroupsJson);
      if (decoded is! List) return const [];
      final groups = <String>[];
      var total = 0;
      for (final value in decoded) {
        if (value is! String ||
            value.trim().isEmpty ||
            groups.contains(value)) {
          continue;
        }
        total += value.length;
        if (groups.length >= 8 || total > 12000) return const [];
        groups.add(value);
      }
      return groups;
    } catch (_) {
      return const [];
    }
  }
}

// 制卡产出的草稿(还没入库的),AI生成和离线规则都吐这个
class DraftCard {
  final String type;
  final String front;
  final String back;
  final int? page;
  final String evidenceQuote;
  final String sourceContext;
  final List<String> evidenceGroups;
  final int? evidenceStart;
  final int? evidenceEnd;

  DraftCard({
    required this.type,
    required this.front,
    required this.back,
    this.page,
    this.evidenceQuote = '',
    this.sourceContext = '',
    this.evidenceGroups = const [],
    this.evidenceStart,
    this.evidenceEnd,
  });
}

class CardAuditResult {
  final String schemaVersion;
  final String label;
  final double errorProbability;
  final String decision;
  final List<String> evidenceQuotes;
  final String reason;
  final String? repairedFront;
  final String? repairedBack;

  CardAuditResult({
    required this.schemaVersion,
    required this.label,
    required this.errorProbability,
    required this.decision,
    required this.evidenceQuotes,
    required this.reason,
    this.repairedFront,
    this.repairedBack,
  });
}

// 试题的五种题型
class QuizType {
  static const single = 'single'; // 单选
  static const judge = 'judge'; // 判断
  static const blank = 'blank'; // 填空
  static const short = 'short'; // 简答(概念/小段知识点)
  static const essay = 'essay'; // 论述(大题)
  static const all = [single, judge, blank, short, essay];

  static String label(String t) => switch (t) {
    single => '单选',
    judge => '判断',
    blank => '填空',
    short => '简答',
    essay => '论述',
    _ => t,
  };

  // 简答和论述算主观题,没有标准答案,得靠批改或者自评给分
  static bool subjective(String t) => t == short || t == essay;
}

// 出题产出的草稿试题,过完校验才入库
class QuizDraft {
  final String qtype;
  final String stem;
  final List<String> options; // 仅单选用,不带 A./B. 前缀
  final String answer; // single: A-D;judge: 对/错;short/essay: 参考答案
  final String explanation;
  final int? page;

  QuizDraft({
    required this.qtype,
    required this.stem,
    this.options = const [],
    required this.answer,
    this.explanation = '',
    this.page,
  });
}

// 题库里存着的一道题
class QuizQuestion {
  final int id;
  final int bookId;
  final int? chapterId;
  final String qtype;
  final String stem;
  final List<String> options;
  final String answer;
  final String explanation;
  final int? sourcePage;
  final String createdAt;
  final double? qStability;
  final double? qDifficulty;
  final int qState;
  final int qStep;
  final DateTime? qLastReview;
  final DateTime? qDue;
  final int qReps;
  final int qLapses;
  final String? qFirstReviewedDay;

  QuizQuestion.fromMap(Map<String, Object?> m)
    : id = m['id'] as int,
      bookId = m['book_id'] as int,
      chapterId = m['chapter_id'] as int?,
      qtype = m['qtype'] as String,
      stem = m['stem'] as String,
      options = [
        for (final o in jsonDecode((m['options'] as String?) ?? '[]') as List)
          o.toString(),
      ],
      answer = m['answer'] as String,
      explanation = (m['explanation'] as String?) ?? '',
      sourcePage = m['source_page'] as int?,
      createdAt = m['created_at'] as String,
      qStability = (m['q_stability'] as num?)?.toDouble(),
      qDifficulty = (m['q_difficulty'] as num?)?.toDouble(),
      qState = (m['q_state'] as int?) ?? CardState.newCard,
      qStep = (m['q_step'] as int?) ?? 0,
      qLastReview = m['q_last_review'] == null
          ? null
          : DateTime.parse(m['q_last_review'] as String),
      qDue = m['q_due'] == null ? null : DateTime.parse(m['q_due'] as String),
      qReps = (m['q_reps'] as int?) ?? 0,
      qLapses = (m['q_lapses'] as int?) ?? 0,
      qFirstReviewedDay = m['q_first_reviewed_day'] as String?;
}

// AI批改主观题给回来的结果
class GradeResult {
  final int score; // 0-10
  final String comment;
  final List<String> missing; // 缺失的要点

  GradeResult({
    required this.score,
    required this.comment,
    this.missing = const [],
  });
}

// 习题集里的一条:做过的题+最近一次的作答结果
class AttemptedQuestion {
  final QuizQuestion question;
  final bool? correct; // 客观题:对/错;主观题为 null
  final int? score; // 主观题 0-10;客观题/未评分为 null
  final String userAnswer;
  final String attemptedAt;

  AttemptedQuestion.fromMap(Map<String, Object?> m)
    : question = QuizQuestion.fromMap(m),
      correct = m['a_correct'] == null ? null : (m['a_correct'] as int) == 1,
      score = m['a_score'] as int?,
      userAnswer = (m['a_user_answer'] as String?) ?? '',
      attemptedAt = (m['a_created_at'] as String?) ?? '';

  // 什么算错题:客观题答错的,或者主观题6分以下的
  bool get wrong => correct == false || (score != null && score! < 6);
}
