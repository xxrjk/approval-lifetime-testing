// 基于 sqflite 插件（pub.dev）的 SQLite 持久化层。
// SQL 查询和更新统一通过 Db 接口执行。
library;

import 'dart:convert';
import 'dart:math';

import 'package:path/path.dart' as p;
import 'package:sqflite/sqflite.dart';

import 'audit_binding.dart';
import 'fsrs.dart';
import 'models.dart';

const _schema = <String>[
  '''
CREATE TABLE books(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT NOT NULL,
  filename TEXT NOT NULL,
  total_pages INTEGER NOT NULL DEFAULT 0,
  scanned INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
)''',
  '''
CREATE TABLE chapters(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL,
  page_start INTEGER NOT NULL,
  page_end INTEGER NOT NULL,
  char_count INTEGER NOT NULL DEFAULT 0
)''',
  '''
CREATE TABLE cards(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
  type TEXT NOT NULL DEFAULT 'qa',
  front TEXT NOT NULL,
  back TEXT NOT NULL,
  source_page INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',
  stability REAL,
  difficulty REAL,
  state INTEGER NOT NULL DEFAULT 0,
  step INTEGER NOT NULL DEFAULT 0,
  last_review TEXT,
  due TEXT NOT NULL,
  reps INTEGER NOT NULL DEFAULT 0,
  lapses INTEGER NOT NULL DEFAULT 0,
  first_reviewed_day TEXT,
  created_at TEXT NOT NULL
)''',
  '''
CREATE TABLE review_logs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL REFERENCES cards(id) ON DELETE CASCADE,
  rating INTEGER NOT NULL,
  reviewed_at TEXT NOT NULL
)''',
  'CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT NOT NULL)',
  "CREATE INDEX idx_cards_book_status ON cards(book_id, status)",
  "CREATE INDEX idx_cards_due ON cards(status, state, due)",
];

// v2加的表:出题的题库 + 问书的聊天记录
const _schemaV2 = <String>[
  '''
CREATE TABLE quiz_questions(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  chapter_id INTEGER REFERENCES chapters(id) ON DELETE SET NULL,
  qtype TEXT NOT NULL,
  stem TEXT NOT NULL,
  options TEXT NOT NULL DEFAULT '[]',
  answer TEXT NOT NULL,
  explanation TEXT NOT NULL DEFAULT '',
  source_page INTEGER,
  created_at TEXT NOT NULL
)''',
  '''
CREATE TABLE chat_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at TEXT NOT NULL
)''',
  'CREATE INDEX idx_quiz_book ON quiz_questions(book_id, qtype)',
  'CREATE INDEX idx_chat_book ON chat_messages(book_id, id)',
];

// v4:书的软删除(回收站)。deleted_at 非空=在回收站里,
// 队列/统计/书库都当这本书不存在,但数据一字不动,随时能恢复
const _schemaV4 = <String>['ALTER TABLE books ADD COLUMN deleted_at TEXT'];

// v5:两件事。
// 1) review_logs 加答题用时(毫秒),统计页的「今日时长」用;老日志补0。
// 2) books 加 text_layer:扫描版是否带可用的 OCR 文字层(文字制卡的备用路径)。
//    老的文字版书都算有文字层;老的扫描版导入时把文字丢了,判不出来,保守按无处理
const _schemaV5 = <String>[
  'ALTER TABLE review_logs ADD COLUMN duration_ms INTEGER NOT NULL DEFAULT 0',
  'ALTER TABLE books ADD COLUMN text_layer INTEGER NOT NULL DEFAULT 0',
  'UPDATE books SET text_layer=1 WHERE scanned=0',
];

// v6:review_logs 增加本地日键,所有"今天"查询统一用 day_key。
const _schemaV6 = <String>['ALTER TABLE review_logs ADD COLUMN day_key TEXT'];

// v7:给题库题目加一组轻量 FSRS 字段,用于习题集的错题复习调度。
// q_state=0 仍表示未进入习题复习队列,和 CardState.newCard 共用数值。
const _schemaV7 = <String>[
  'ALTER TABLE quiz_questions ADD COLUMN q_stability REAL',
  'ALTER TABLE quiz_questions ADD COLUMN q_difficulty REAL',
  'ALTER TABLE quiz_questions ADD COLUMN q_state INTEGER NOT NULL DEFAULT 0',
  'ALTER TABLE quiz_questions ADD COLUMN q_step INTEGER NOT NULL DEFAULT 0',
  'ALTER TABLE quiz_questions ADD COLUMN q_due TEXT',
  'ALTER TABLE quiz_questions ADD COLUMN q_last_review TEXT',
  'ALTER TABLE quiz_questions ADD COLUMN q_reps INTEGER NOT NULL DEFAULT 0',
  'ALTER TABLE quiz_questions ADD COLUMN q_lapses INTEGER NOT NULL DEFAULT 0',
  'ALTER TABLE quiz_questions ADD COLUMN q_first_reviewed_day TEXT',
  'CREATE INDEX idx_quiz_fsrs_book_due ON quiz_questions(book_id, q_state, q_due)',
];

// v8:研究版证据与审计字段。source_evidence 是生成时从教材逐字复制的原文,
// audit_priority=P(error)*未来预计复习次数，用于审计优先级排序。
const _schemaV8 = <String>[
  "ALTER TABLE cards ADD COLUMN source_evidence TEXT NOT NULL DEFAULT ''",
  "ALTER TABLE cards ADD COLUMN source_context TEXT NOT NULL DEFAULT ''",
  'ALTER TABLE cards ADD COLUMN source_evidence_start INTEGER',
  'ALTER TABLE cards ADD COLUMN source_evidence_end INTEGER',
  "ALTER TABLE cards ADD COLUMN audit_status TEXT NOT NULL DEFAULT 'not_audited'",
  'ALTER TABLE cards ADD COLUMN audit_error_probability REAL',
  'ALTER TABLE cards ADD COLUMN audit_expected_reviews REAL NOT NULL DEFAULT 1.0',
  'ALTER TABLE cards ADD COLUMN audit_priority REAL',
  "ALTER TABLE cards ADD COLUMN audit_quotes TEXT NOT NULL DEFAULT '[]'",
  'ALTER TABLE cards ADD COLUMN audit_model TEXT',
  'ALTER TABLE cards ADD COLUMN audit_updated_at TEXT',
  'ALTER TABLE cards ADD COLUMN audit_reason TEXT',
  'CREATE INDEX idx_cards_audit_priority ON cards(status, audit_priority)',
];

// v9:保存独立本地审计器返回的答案字符跨度与可复现元数据。
// 跨度是 UTF-16 offset JSON；metadata 保留模型 revision、权重 hash、
// evidence-union 聚合方式和冻结阈值。
const _schemaV9 = <String>[
  "ALTER TABLE cards ADD COLUMN audit_spans TEXT NOT NULL DEFAULT '[]'",
  "ALTER TABLE cards ADD COLUMN audit_metadata TEXT NOT NULL DEFAULT '{}'",
];

// v10:保存生成输入中确定性检索出的多块原文。数组元素均是生成输入的
// 连续原文子串；本地审计实际发送这些块，不再只发送单一锚定窗口。
const _schemaV10 = <String>[
  "ALTER TABLE cards ADD COLUMN source_evidence_groups TEXT NOT NULL DEFAULT '[]'",
];

// v11: persist the exact content binding accepted by an audit. Audit results
// are committed only if this digest still matches inside the same SQLite
// transaction; editing an active card also removes it from the study queue.
const _schemaV11 = <String>[
  'ALTER TABLE cards ADD COLUMN audit_content_hash TEXT',
];

// v12: persist the canonical hosted-audit contract and an append-only record
// for explicit human overrides. Standard activation revalidates these fields
// against the current persisted evidence inside the SQLite transaction.
const _schemaV12 = <String>[
  'ALTER TABLE cards ADD COLUMN audit_schema_version TEXT',
  'ALTER TABLE cards ADD COLUMN audit_answer_label TEXT',
  'ALTER TABLE cards ADD COLUMN audit_decision TEXT',
  '''
CREATE TABLE release_override_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_id INTEGER NOT NULL,
  book_id INTEGER NOT NULL,
  action TEXT NOT NULL,
  actor TEXT NOT NULL,
  reason TEXT NOT NULL,
  prior_card_status TEXT NOT NULL,
  prior_audit_status TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  audit_content_hash TEXT,
  day_key TEXT NOT NULL,
  created_at TEXT NOT NULL
)''',
  'CREATE INDEX idx_release_override_card ON release_override_events(card_id, id)',
  'CREATE INDEX idx_release_override_day ON release_override_events(day_key, card_id)',
];

// Explicit authority revocation is separate from reversible content editing.
// Existing bound approvals migrate to epoch zero and keep their prior checks.
const _schemaV13 = <String>[
  'ALTER TABLE cards ADD COLUMN approval_epoch INTEGER NOT NULL DEFAULT 0',
  'ALTER TABLE cards ADD COLUMN audit_approval_epoch INTEGER',
  'UPDATE cards SET audit_approval_epoch=0 WHERE audit_content_hash IS NOT NULL',
];

// v3加的表:答题记录(习题集用)+ 问答收藏
// 注意升级顺序是链式的,v1的老用户会先补v2再补v3
const _schemaV3 = <String>[
  '''
CREATE TABLE quiz_attempts(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  question_id INTEGER NOT NULL REFERENCES quiz_questions(id) ON DELETE CASCADE,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  correct INTEGER,
  score INTEGER,
  user_answer TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
)''',
  '''
CREATE TABLE saved_answers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  created_at TEXT NOT NULL
)''',
  'CREATE INDEX idx_attempts_q ON quiz_attempts(question_id, id)',
  'CREATE INDEX idx_attempts_book ON quiz_attempts(book_id)',
  'CREATE INDEX idx_saved_book ON saved_answers(book_id, id)',
];

class Db {
  // 这俩是给单测用的,测试里换成 sqflite_common_ffi 的内存库
  static DatabaseFactory? factoryOverride;
  static String? pathOverride;

  static Database? _db;

  static Future<Database> get db async {
    if (_db != null) return _db!;
    final factory = factoryOverride ?? databaseFactory;
    final path = pathOverride ?? p.join(await getDatabasesPath(), 'approval_sample.db');
    _db = await factory.openDatabase(
      path,
      options: OpenDatabaseOptions(
        version: 13,
        onConfigure: (d) => d.execute('PRAGMA foreign_keys=ON'),
        onCreate: (d, v) async {
          for (final s in [
            ..._schema,
            ..._schemaV2,
            ..._schemaV3,
            ..._schemaV4,
            ..._schemaV5,
            ..._schemaV6,
            ..._schemaV7,
            ..._schemaV8,
            ..._schemaV9,
            ..._schemaV10,
            ..._schemaV11,
            ..._schemaV12,
            ..._schemaV13,
          ]) {
            await d.execute(s);
          }
        },
        onUpgrade: (d, oldV, newV) async {
          if (oldV < 2) {
            for (final s in _schemaV2) {
              await d.execute(s);
            }
          }
          if (oldV < 3) {
            for (final s in _schemaV3) {
              await d.execute(s);
            }
          }
          if (oldV < 4) {
            for (final s in _schemaV4) {
              await d.execute(s);
            }
          }
          if (oldV < 5) {
            for (final s in _schemaV5) {
              await d.execute(s);
            }
          }
          if (oldV < 6) {
            for (final s in _schemaV6) {
              await d.execute(s);
            }
            await _backfillReviewLogDayKeys(d);
          }
          if (oldV < 7) {
            for (final s in _schemaV7) {
              await d.execute(s);
            }
          }
          if (oldV < 8) {
            for (final s in _schemaV8) {
              await d.execute(s);
            }
          }
          if (oldV < 9) {
            for (final s in _schemaV9) {
              await d.execute(s);
            }
          }
          if (oldV < 10) {
            for (final s in _schemaV10) {
              await d.execute(s);
            }
          }
          if (oldV < 11) {
            for (final s in _schemaV11) {
              await d.execute(s);
            }
          }
          if (oldV < 12) {
            for (final s in _schemaV12) {
              await d.execute(s);
            }
          }
          if (oldV < 13) {
            for (final s in _schemaV13) {
              await d.execute(s);
            }
          }
        },
      ),
    );
    return _db!;
  }

  // 测试用,关掉连接重来
  static Future<void> reset() async {
    await _db?.close();
    _db = null;
  }

  static String nowIso() => DateTime.now().toUtc().toIso8601String();

  static DateTime todayEndUtc() {
    final now = DateTime.now();
    return DateTime(now.year, now.month, now.day + 1).toUtc();
  }

  static String _dayKey(DateTime d) =>
      '${d.year.toString().padLeft(4, '0')}-'
      '${d.month.toString().padLeft(2, '0')}-'
      '${d.day.toString().padLeft(2, '0')}';

  // 本地日期 yyyy-MM-dd,算每日新卡限额的时候按这个分天
  static String localDay([DateTime? t]) {
    return _dayKey(t ?? DateTime.now());
  }

  static Future<void> _backfillReviewLogDayKeys(Database d) async {
    final rows = await d.rawQuery(
      'SELECT id, reviewed_at FROM review_logs WHERE day_key IS NULL',
    );
    if (rows.isEmpty) return;
    final batch = d.batch();
    for (final r in rows) {
      final utc = DateTime.parse(r['reviewed_at'] as String);
      batch.update(
        'review_logs',
        {'day_key': localDay(utc.toLocal())},
        where: 'id=?',
        whereArgs: [r['id']],
      );
    }
    await batch.commit(noResult: true);
  }

  // ---------- 设置 ----------

  static Future<String?> getKv(String key) async {
    final rows = await (await db).query(
      'kv',
      where: 'key=?',
      whereArgs: [key],
      limit: 1,
    );
    return rows.isEmpty ? null : rows.first['value'] as String;
  }

  static Future<void> setKv(String key, String value) async {
    await (await db).insert('kv', {
      'key': key,
      'value': value,
    }, conflictAlgorithm: ConflictAlgorithm.replace);
  }

  // ---------- 书 / 章节 ----------

  // textLayer:扫描版是否带可用的 OCR 文字层。不传的话按"文字版有、扫描版无"处理
  static Future<int> insertBook(
    String title,
    String filename,
    int totalPages,
    bool scanned, {
    bool? textLayer,
  }) async {
    return (await db).insert('books', {
      'title': title,
      'filename': filename,
      'total_pages': totalPages,
      'scanned': scanned ? 1 : 0,
      'text_layer': (textLayer ?? !scanned) ? 1 : 0,
      'created_at': nowIso(),
    });
  }

  static Future<void> insertChapters(
    int bookId,
    List<Map<String, Object?>> chapters,
  ) async {
    final d = await db;
    final batch = d.batch();
    var idx = 0;
    for (final ch in chapters) {
      batch.insert('chapters', {
        'book_id': bookId,
        'idx': idx++,
        'title': ch['title'],
        'page_start': ch['page_start'],
        'page_end': ch['page_end'],
        'char_count': ch['char_count'] ?? 0,
      });
    }
    await batch.commit(noResult: true);
  }

  // 书列表,顺带把每本书的各种卡片数一起查出来,书库页要显示
  static Future<List<Map<String, Object?>>> listBooks() async {
    final now = nowIso();
    return (await db).rawQuery(
      '''
SELECT b.*,
 (SELECT COUNT(*) FROM cards c WHERE c.book_id=b.id AND c.status='pending') AS pending_cnt,
 (SELECT COUNT(*) FROM cards c WHERE c.book_id=b.id AND c.status='active' AND c.state=0) AS new_cnt,
 (SELECT COUNT(*) FROM cards c WHERE c.book_id=b.id AND c.status='active' AND c.state!=0 AND c.due<=?) AS due_cnt,
 (SELECT COUNT(*) FROM cards c WHERE c.book_id=b.id AND c.status='active') AS active_cnt,
 (SELECT COUNT(*) FROM cards c WHERE c.book_id=b.id AND c.status='active' AND c.state!=0) AS learned_cnt
FROM books b WHERE b.deleted_at IS NULL ORDER BY b.id DESC''',
      [now],
    );
  }

  static Future<Book?> getBook(int id) async {
    final rows = await (await db).query(
      'books',
      where: 'id=?',
      whereArgs: [id],
    );
    return rows.isEmpty ? null : Book.fromMap(rows.first);
  }

  // 某本书的章节列表,带每章的卡片计数
  static Future<List<Map<String, Object?>>> chaptersOf(int bookId) async {
    return (await db).rawQuery(
      '''
SELECT ch.*,
 (SELECT COUNT(*) FROM cards c WHERE c.chapter_id=ch.id AND c.status='pending') AS pending_cnt,
 (SELECT COUNT(*) FROM cards c WHERE c.chapter_id=ch.id AND c.status!='pending') AS done_cnt
FROM chapters ch WHERE ch.book_id=? ORDER BY ch.idx''',
      [bookId],
    );
  }

  // 左滑删除走这里:软删除进回收站,数据一字不动
  static Future<void> softDeleteBook(int id) async {
    await (await db).update(
      'books',
      {'deleted_at': nowIso()},
      where: 'id=?',
      whereArgs: [id],
    );
  }

  static Future<void> restoreBook(int id) async {
    await (await db).update(
      'books',
      {'deleted_at': null},
      where: 'id=?',
      whereArgs: [id],
    );
  }

  // 回收站列表,带卡片数,删得晚的排前面
  static Future<List<Map<String, Object?>>> trashedBooks() async {
    return (await db).rawQuery('''
SELECT b.*,
 (SELECT COUNT(*) FROM cards c WHERE c.book_id=b.id) AS card_cnt
FROM books b WHERE b.deleted_at IS NOT NULL ORDER BY b.deleted_at DESC''');
  }

  // 真删除,只给回收站的「彻底删除」用。卡片/题库/记录全部级联删掉,回不来
  static Future<void> deleteBook(int id) async {
    await (await db).delete('books', where: 'id=?', whereArgs: [id]);
  }

  // ---------- 卡片 ----------

  static Future<void> insertDraftCards(
    int bookId,
    int? chapterId,
    List<DraftCard> drafts,
  ) async {
    if (drafts.isEmpty) return;
    final d = await db;
    final now = nowIso();
    final batch = d.batch();
    for (final c in drafts) {
      batch.insert('cards', {
        'book_id': bookId,
        'chapter_id': chapterId,
        'type': c.type,
        'front': c.front,
        'back': c.back,
        'source_page': c.page,
        'source_evidence': c.evidenceQuote,
        'source_context': c.sourceContext,
        'source_evidence_groups': jsonEncode(c.evidenceGroups),
        'source_evidence_start': c.evidenceStart,
        'source_evidence_end': c.evidenceEnd,
        'audit_status': c.evidenceQuote.isEmpty
            ? AuditStatus.notAudited
            : AuditStatus.evidenceAnchored,
        'status': CardStatus.pending,
        'state': CardState.newCard,
        'step': 0,
        'due': now,
        'reps': 0,
        'lapses': 0,
        'created_at': now,
      });
    }
    await batch.commit(noResult: true);
  }

  static Future<List<CardItem>> pendingCards(
    int bookId, {
    int? chapterId,
  }) async {
    final where = StringBuffer("book_id=? AND status='pending'");
    final args = <Object?>[bookId];
    if (chapterId != null) {
      where.write(' AND chapter_id=?');
      args.add(chapterId);
    }
    final rows = await (await db).query(
      'cards',
      where: where.toString(),
      whereArgs: args,
      orderBy: 'COALESCE(audit_error_probability, -1) DESC, id',
    );
    return rows.map(CardItem.fromMap).toList();
  }

  static Future<void> updateCardText(int id, String front, String back) async {
    await (await db).update(
      'cards',
      {
        'front': front,
        'back': back,
        // Editing changes the release object. Even an already-active card is
        // pulled back behind the boundary until it is re-audited or explicitly
        // force-approved by the user.
        'status': CardStatus.pending,
        'audit_status': AuditStatus.stale,
        'audit_error_probability': null,
        'audit_priority': null,
        'audit_quotes': '[]',
        'audit_model': null,
        'audit_updated_at': null,
        'audit_reason': null,
        'audit_spans': '[]',
        'audit_metadata': '{}',
        'audit_content_hash': null,
        'audit_schema_version': null,
        'audit_answer_label': null,
        'audit_decision': null,
        'audit_approval_epoch': null,
      },
      where: 'id=?',
      whereArgs: [id],
    );
  }

  static List<String> _persistedEvidenceGroups(CardItem card) {
    if (card.sourceEvidenceGroups.isNotEmpty) {
      return card.sourceEvidenceGroups;
    }
    final fallback = card.sourceContext.isNotEmpty
        ? card.sourceContext
        : card.sourceEvidence;
    return [if (fallback.trim().isNotEmpty) fallback];
  }

  static List<String>? _decodeAuditQuotes(String source) {
    try {
      final value = jsonDecode(source);
      if (value is! List || value.any((item) => item is! String)) return null;
      return value.cast<String>();
    } catch (_) {
      return null;
    }
  }

  static bool _quotesBelongToIndividualBlocks(
    List<String> quotes,
    List<String> groups,
  ) =>
      quotes.isNotEmpty &&
      quotes.every(
        (quote) =>
            quote.trim().isNotEmpty &&
            groups.any((group) => group.contains(quote)),
      );

  static bool _standardAuditContractHolds(CardItem card) {
    final quotes = _decodeAuditQuotes(card.auditQuotesJson);
    final observedHash = cardAuditContentHash(card);
    final probability = card.auditErrorProbability;
    return card.auditStatus == AuditStatus.passed &&
        card.auditSchemaVersion == CardAuditContract.schemaVersion &&
        card.auditAnswerLabel == CardAuditContract.supported &&
        card.auditDecision == CardAuditContract.pass &&
        card.auditContentHash == observedHash &&
        card.auditApprovalEpoch == card.approvalEpoch &&
        (probability == null ||
            (probability.isFinite && probability >= 0 && probability <= 1)) &&
        card.expectedReviews.isFinite &&
        card.expectedReviews >= 0 &&
        (card.auditModel?.trim().isNotEmpty ?? false) &&
        (card.auditReason?.trim().isNotEmpty ?? false) &&
        quotes != null &&
        _quotesBelongToIndividualBlocks(quotes, _persistedEvidenceGroups(card));
  }

  static Future<bool> updateCardAudit({
    required int id,
    required String auditStatus,
    required double? errorProbability,
    required double expectedReviews,
    required List<String> evidenceQuotes,
    required String model,
    required String reason,
    List<AuditSpan> answerSpans = const [],
    Map<String, Object?> metadata = const {},
    String? expectedContentHash,
    int expectedApprovalEpoch = 0,
    String? schemaVersion,
    String? answerLabel,
    String? decision,
  }) async {
    if (errorProbability != null &&
        (!errorProbability.isFinite ||
            errorProbability < 0 ||
            errorProbability > 1)) {
      throw ArgumentError.value(
        errorProbability,
        'errorProbability',
        'must be in [0,1]',
      );
    }
    if (!expectedReviews.isFinite || expectedReviews < 0) {
      throw ArgumentError.value(
        expectedReviews,
        'expectedReviews',
        'must be non-negative',
      );
    }
    final d = await db;
    return d.transaction((txn) async {
      final rows = await txn.query('cards', where: 'id=?', whereArgs: [id]);
      if (rows.isEmpty) return false;
      final current = CardItem.fromMap(rows.single);
      final observedHash = cardAuditContentHash(current);
      if (expectedApprovalEpoch != current.approvalEpoch) return false;
      if (expectedContentHash != null && observedHash != expectedContentHash) {
        // This response has no authority over the current object. Reject it
        // without clearing a newer approval committed for the current content.
        // The edit path already invalidates approval when content changes.
        return false;
      }
      if (auditStatus == AuditStatus.passed) {
        if (expectedContentHash == null ||
            schemaVersion != CardAuditContract.schemaVersion ||
            answerLabel != CardAuditContract.supported ||
            decision != CardAuditContract.pass ||
            model.trim().isEmpty ||
            reason.trim().isEmpty ||
            !_quotesBelongToIndividualBlocks(
              evidenceQuotes,
              _persistedEvidenceGroups(current),
            )) {
          return false;
        }
      }
      final updated = await txn.update(
        'cards',
        {
          'audit_status': auditStatus,
          'audit_error_probability': errorProbability,
          'audit_expected_reviews': expectedReviews,
          'audit_priority': errorProbability == null
              ? null
              : errorProbability * expectedReviews,
          'audit_quotes': jsonEncode(evidenceQuotes),
          'audit_model': model,
          'audit_updated_at': nowIso(),
          'audit_reason': reason,
          'audit_spans': jsonEncode([
            for (final span in answerSpans) span.toMap(),
          ]),
          'audit_metadata': jsonEncode(metadata),
          'audit_content_hash': observedHash,
          'audit_approval_epoch': current.approvalEpoch,
          'audit_schema_version': schemaVersion,
          'audit_answer_label': answerLabel,
          'audit_decision': decision,
        },
        where: 'id=? AND status=?',
        whereArgs: [id, CardStatus.pending],
      );
      return updated == 1;
    });
  }

  /// Explicitly revoke all receipts captured before this operation.
  /// Text editing deliberately does not advance this authority epoch.
  static Future<bool> revokeCardApproval(int id) async {
    final d = await db;
    return d.transaction((txn) async {
      final changed = await txn.rawUpdate(
        '''
UPDATE cards SET approval_epoch=approval_epoch+1, status='pending',
 audit_status='stale', audit_content_hash=NULL, audit_approval_epoch=NULL,
 audit_schema_version=NULL, audit_answer_label=NULL, audit_decision=NULL,
 audit_quotes='[]', audit_metadata='{}', audit_spans='[]',
 audit_error_probability=NULL, audit_priority=NULL, audit_model=NULL,
 audit_reason=NULL, audit_updated_at=NULL
WHERE id=?''',
        [id],
      );
      return changed == 1;
    });
  }

  static const Map<String, Set<String>> _standardStatusTransitions = {
    CardStatus.pending: {CardStatus.active, CardStatus.suspended},
    CardStatus.active: {CardStatus.suspended},
    CardStatus.suspended: {CardStatus.active},
  };

  static Future<bool> updateCardStatus(int id, String status) async {
    final d = await db;
    return d.transaction((txn) async {
      final rows = await txn.query('cards', where: 'id=?', whereArgs: [id]);
      if (rows.isEmpty) return false;
      final current = CardItem.fromMap(rows.single);

      final allowedTargets = _standardStatusTransitions[current.status];
      if (allowedTargets == null || !allowedTargets.contains(status)) {
        return false;
      }

      // Every standard transition into scheduler eligibility is mediated by
      // the same persisted audit contract. This includes restoration from a
      // suspended state; explicit overrides use forceApprovePendingCards.
      if (status == CardStatus.active &&
          !_standardAuditContractHolds(current)) {
        return false;
      }

      final updated = await txn.update(
        'cards',
        {'status': status},
        where: 'id=? AND status=?',
        whereArgs: [id, current.status],
      );
      return updated == 1;
    });
  }

  static Future<Set<int>> forceAddedToday() async {
    final rows = await (await db).query(
      'release_override_events',
      columns: ['card_id'],
      where: 'day_key=?',
      whereArgs: [localDay()],
    );
    final ids = {for (final row in rows) row['card_id']! as int};
    // Preserve markers created by pre-v12 builds; new overrides are recorded
    // as append-only events below.
    final legacy = await getKv('force_added_${localDay()}');
    if (legacy == null || legacy.isEmpty) return ids;
    ids.addAll({
      for (final part in legacy.split(','))
        if (int.tryParse(part) != null) int.parse(part),
    });
    return ids;
  }

  // Compatibility helper for existing callers that mark an already-active
  // card as a same-day explicit override. New release actions should call
  // forceApprovePendingCards so status change and event append are atomic.
  static Future<void> addForceAdded(List<int> cardIds) async {
    if (cardIds.isEmpty) return;
    final d = await db;
    await d.transaction((txn) async {
      for (final id in cardIds.toSet()) {
        final rows = await txn.query('cards', where: 'id=?', whereArgs: [id]);
        if (rows.isEmpty) continue;
        final card = CardItem.fromMap(rows.single);
        await _appendOverrideEvent(
          txn,
          card: card,
          action: 'same_day_force_marker',
          actor: 'local_user',
          reason: 'explicit same-day scheduling override',
        );
      }
    });
  }

  static Future<void> _appendOverrideEvent(
    DatabaseExecutor txn, {
    required CardItem card,
    required String action,
    required String actor,
    required String reason,
  }) async {
    await txn.insert('release_override_events', {
      'card_id': card.id,
      'book_id': card.bookId,
      'action': action,
      'actor': actor,
      'reason': reason,
      'prior_card_status': card.status,
      'prior_audit_status': card.auditStatus,
      'content_hash': cardAuditContentHash(card),
      'audit_content_hash': card.auditContentHash,
      'day_key': localDay(),
      'created_at': nowIso(),
    });
  }

  static Future<List<Map<String, Object?>>> releaseOverrideEventsForCard(
    int cardId,
  ) => (db).then(
    (d) => d.query(
      'release_override_events',
      where: 'card_id=?',
      whereArgs: [cardId],
      orderBy: 'id',
    ),
  );

  static Future<int> forceApprovePendingCards(
    List<int> cardIds, {
    String actor = 'local_user',
    String reason = 'explicit force-add from proof screen',
  }) async {
    if (cardIds.isEmpty) return 0;
    final ids = cardIds.toSet().toList()..sort();
    final placeholders = ids.map((_) => '?').join(',');
    final d = await db;
    return d.transaction((txn) async {
      final rows = await txn.query(
        'cards',
        where: "status='pending' AND id IN ($placeholders)",
        whereArgs: ids,
        orderBy: 'id',
      );
      if (rows.isEmpty) return 0;
      final cards = rows.map(CardItem.fromMap).toList();
      final releasedIds = [for (final card in cards) card.id];
      final releasePlaceholders = releasedIds.map((_) => '?').join(',');
      final n = await txn.rawUpdate(
        "UPDATE cards SET status='active' WHERE status='pending' "
        'AND id IN ($releasePlaceholders)',
        releasedIds,
      );
      if (n != releasedIds.length) {
        throw StateError('override status update was not atomic');
      }
      for (final card in cards) {
        await _appendOverrideEvent(
          txn,
          card: card,
          action: 'force_activate',
          actor: actor,
          reason: reason,
        );
      }
      return n;
    });
  }

  static Future<void> deleteCard(int id) async {
    await (await db).delete('cards', where: 'id=?', whereArgs: [id]);
  }

  // 一键通过所有待校对的卡,返回通过了几张
  static Future<int> approvePending(int bookId, {int? chapterId}) async {
    final d = await db;
    return d.transaction((txn) async {
      final where = StringBuffer(
        "book_id=? AND status='pending' AND audit_status='passed'",
      );
      final args = <Object?>[bookId];
      if (chapterId != null) {
        where.write(' AND chapter_id=?');
        args.add(chapterId);
      }
      final rows = await txn.query(
        'cards',
        where: where.toString(),
        whereArgs: args,
      );
      final releasable = <int>[];
      final stale = <int>[];
      for (final row in rows) {
        final card = CardItem.fromMap(row);
        if (_standardAuditContractHolds(card)) {
          releasable.add(card.id);
        } else {
          stale.add(card.id);
        }
      }
      if (stale.isNotEmpty) {
        final placeholders = stale.map((_) => '?').join(',');
        await txn.rawUpdate(
          "UPDATE cards SET audit_status='stale', audit_content_hash=NULL, "
          'audit_schema_version=NULL, audit_answer_label=NULL, '
          'audit_decision=NULL '
          'WHERE id IN ($placeholders)',
          stale,
        );
      }
      if (releasable.isEmpty) return 0;
      final placeholders = releasable.map((_) => '?').join(',');
      return txn.rawUpdate(
        "UPDATE cards SET status='active' WHERE status='pending' "
        'AND id IN ($placeholders)',
        releasable,
      );
    });
  }

  // ---------- 背诵队列 ----------

  // 今天已经开背了几张新卡(算限额用)
  static Future<int> introducedToday() async {
    final forceIds = await forceAddedToday();
    final args = <Object?>[localDay()];
    var extra = '';
    if (forceIds.isNotEmpty) {
      final ids = forceIds.toList()..sort();
      extra = ' AND id NOT IN (${ids.map((_) => '?').join(',')})';
      args.addAll(ids);
    }
    final rows = await (await db).rawQuery(
      'SELECT COUNT(*) AS n FROM cards '
      'WHERE first_reviewed_day=? '
      'AND book_id IN (SELECT id FROM books WHERE deleted_at IS NULL)'
      '$extra',
      args,
    );
    return rows.first['n'] as int;
  }

  static Future<int> dueCount() async {
    final rows = await (await db).rawQuery(
      "SELECT COUNT(*) AS n FROM cards WHERE status='active' AND state!=0 AND due<=? AND book_id IN (SELECT id FROM books WHERE deleted_at IS NULL)",
      [nowIso()],
    );
    return rows.first['n'] as int;
  }

  static Future<int> newTotal() async {
    final rows = await (await db).rawQuery(
      "SELECT COUNT(*) AS n FROM cards WHERE status='active' AND state=0 AND book_id IN (SELECT id FROM books WHERE deleted_at IS NULL)",
    );
    return rows.first['n'] as int;
  }

  // 今日已背了几张卡。口径是"今天碰过,且今天不会再回流"的卡片数。
  // 学习步里 1/10 分钟后还会弹出的卡不算完成;毕业到明天以后才计入。
  static Future<int> reviewedToday() async {
    final rows = await (await db).rawQuery(
      'SELECT COUNT(DISTINCT rl.card_id) AS n '
      'FROM review_logs rl JOIN cards c ON c.id=rl.card_id '
      'WHERE rl.day_key=? AND c.due>?',
      [localDay(), todayEndUtc().toIso8601String()],
    );
    return rows.first['n'] as int;
  }

  // 今日接触过几张不同卡。总量上限用这个口径,学习步内重复同一张不重复扣额度。
  static Future<int> _uniqueCardsStudiedToday() async {
    final rows = await (await db).rawQuery(
      'SELECT COUNT(DISTINCT card_id) AS n FROM review_logs WHERE day_key=?',
      [localDay()],
    );
    return rows.first['n'] as int;
  }

  // 学习中的卡最多提前多久拿出来背(Anki的learn-ahead)。
  // 点了「忘记」的卡1/10分钟后到期,没有这个窗口的话背完新卡队列一空,
  // 这些卡就要干等到点,看起来像"点一次忘记就过去了"
  static const learnAheadMinutes = 20;

  // 取下一张该背的卡,没有就返回null。
  // 优先级:先清到期的复习卡(按到期时间排),然后上新卡(受每日限额管着),
  // 最后把20分钟内就要到期的学习中/重学中的卡提前拿出来,保证「忘记」的卡当场重复到记住为止
  static Future<Map<String, Object?>?> nextStudyCard(
    int newPerDay, {
    int maxTotalPerDay = 0,
  }) async {
    final d = await db;
    final now = nowIso();
    const joinSel = '''
SELECT c.*, b.title AS book_title, b.filename AS book_filename, ch.title AS chapter_title
FROM cards c
JOIN books b ON b.id=c.book_id
LEFT JOIN chapters ch ON ch.id=c.chapter_id''';
    final due = await d.rawQuery(
      '''
$joinSel
WHERE b.deleted_at IS NULL AND c.status='active' AND c.state!=0 AND c.due<=?
ORDER BY c.due LIMIT 1''',
      [now],
    );
    if (due.isNotEmpty) return due.first;

    final forceIds = await forceAddedToday();
    if (forceIds.isNotEmpty) {
      final ids = forceIds.toList()..sort();
      final placeholders = ids.map((_) => '?').join(',');
      final forced = await d.rawQuery('''
$joinSel
WHERE b.deleted_at IS NULL AND c.status='active' AND c.state=0 AND c.id IN ($placeholders)
ORDER BY c.id LIMIT 1''', ids);
      if (forced.isNotEmpty) return forced.first;
    }

    final left = newPerDay - await introducedToday();
    final effectiveNew = maxTotalPerDay > 0
        ? min(left, max(0, maxTotalPerDay - await _uniqueCardsStudiedToday()))
        : left;
    if (effectiveNew > 0) {
      final fresh = await d.rawQuery('''
$joinSel
WHERE b.deleted_at IS NULL AND c.status='active' AND c.state=0
ORDER BY c.book_id, c.chapter_id, c.id LIMIT 1''');
      if (fresh.isNotEmpty) return fresh.first;
    }
    final aheadUntil = DateTime.now()
        .add(const Duration(minutes: learnAheadMinutes))
        .toUtc()
        .toIso8601String();
    final ahead = await d.rawQuery(
      '''
$joinSel
WHERE b.deleted_at IS NULL AND c.status='active' AND c.state IN (1,3) AND c.due<=?
ORDER BY c.due LIMIT 1''',
      [aheadUntil],
    );
    if (ahead.isNotEmpty) return ahead.first;
    return null;
  }

  // 队列暂时空了的时候,查最近一张卡几点到期,给用户提示"到点再来"
  static Future<DateTime?> nextDueTime() async {
    final rows = await (await db).rawQuery(
      "SELECT MIN(due) AS d FROM cards WHERE status='active' AND state!=0 AND due>? AND book_id IN (SELECT id FROM books WHERE deleted_at IS NULL)",
      [nowIso()],
    );
    final v = rows.first['d'] as String?;
    if (v == null) return null;
    final t = DateTime.parse(v).toLocal();
    return t.difference(DateTime.now()).inHours < 16 ? t : null;
  }

  // 背诵打分落库:更新卡片状态+记一条复习日志,放一个事务里。
  // durationMs 是这张卡从亮出来到打分的用时,统计「今日时长」用
  static Future<void> applyReview(
    CardItem original,
    int rating, {
    Fsrs? scheduler,
    int durationMs = 0,
  }) async {
    final localNow = DateTime.now();
    final dayKey = localDay(localNow);
    final now = localNow.toUtc();
    final result = (scheduler ?? Fsrs()).review(original, rating, now);
    final card = result.card;
    card.firstReviewedDay ??= dayKey;
    final d = await db;
    await d.transaction((txn) async {
      await txn.update(
        'cards',
        {
          'stability': card.stability,
          'difficulty': card.difficulty,
          'state': card.state,
          'step': card.step,
          'last_review': card.lastReview!.toIso8601String(),
          'due': card.due.toIso8601String(),
          'reps': card.reps,
          'lapses': card.lapses,
          'first_reviewed_day': card.firstReviewedDay,
        },
        where: 'id=?',
        whereArgs: [card.id],
      );
      await txn.insert('review_logs', {
        'card_id': card.id,
        'rating': rating,
        'reviewed_at': now.toIso8601String(),
        'day_key': dayKey,
        'duration_ms': durationMs,
      });
    });
  }

  // ---------- 统计 / 打卡 ----------

  // 复习日志的轻量原始行(card_id/rating/reviewed_at/duration_ms),按时间先后有序。
  // 按本地日聚合、DISTINCT 卡片数、当天最终评分这些都在 stats.dart 里做:
  // SQL 里直接按天分组分的是 UTC 天,跨时区会把半夜的记录算错日。
  // 故意不过滤回收站的书:那天背过就是背过,打卡历史不因为删书而缩水
  static Future<List<Map<String, Object?>>> reviewLogRows() async {
    return (await db).query(
      'review_logs',
      columns: ['card_id', 'rating', 'reviewed_at', 'duration_ms'],
      orderBy: 'id',
    );
  }

  // 未来 days 天内到期的复习卡的到期时间列表(含已逾期),给预测图分桶用
  static Future<List<String>> upcomingDue(int days) async {
    final end = DateTime.now()
        .add(Duration(days: days))
        .toUtc()
        .toIso8601String();
    final rows = await (await db).rawQuery(
      "SELECT due FROM cards WHERE status='active' AND state!=0 AND due<? AND book_id IN (SELECT id FROM books WHERE deleted_at IS NULL)",
      [end],
    );
    return [for (final r in rows) r['due'] as String];
  }

  // 全部可背的卡,给未来复习量的模拟推演用
  static Future<List<CardItem>> activeCards() async {
    final rows = await (await db).query(
      'cards',
      where:
          "status='active' AND book_id IN (SELECT id FROM books WHERE deleted_at IS NULL)",
    );
    return rows.map(CardItem.fromMap).toList();
  }

  // 某章节的全部卡片,待校对的排前面(和校对流程的顺序一致),其余按id
  static Future<List<CardItem>> cardsOfChapter(int chapterId) async {
    final rows = await (await db).query(
      'cards',
      where: 'chapter_id=?',
      whereArgs: [chapterId],
      orderBy: "CASE status WHEN 'pending' THEN 0 ELSE 1 END, id",
    );
    return rows.map(CardItem.fromMap).toList();
  }

  // ---------- 题库 ----------

  // 一批新题入库,把生成的id都返回(出完题直接开练要用)
  static Future<List<int>> insertQuizDrafts(
    int bookId,
    int? chapterId,
    List<QuizDraft> drafts,
  ) async {
    if (drafts.isEmpty) return const [];
    final d = await db;
    final now = nowIso();
    final batch = d.batch();
    for (final q in drafts) {
      batch.insert('quiz_questions', {
        'book_id': bookId,
        'chapter_id': chapterId,
        'qtype': q.qtype,
        'stem': q.stem,
        'options': jsonEncode(q.options),
        'answer': q.answer,
        'explanation': q.explanation,
        'source_page': q.page,
        'created_at': now,
      });
    }
    final ids = await batch.commit();
    return [for (final id in ids) id as int];
  }

  static String _inClause(List<int> ids) =>
      List.filled(ids.length, '?').join(',');

  // 题库按题型数一下有几道,可以只看选中的那几章
  static Future<Map<String, int>> quizCounts(
    int bookId, {
    List<int>? chapterIds,
  }) async {
    var where = 'book_id=?';
    final args = <Object?>[bookId];
    if (chapterIds != null && chapterIds.isNotEmpty) {
      where += ' AND chapter_id IN (${_inClause(chapterIds)})';
      args.addAll(chapterIds);
    }
    final rows = await (await db).rawQuery(
      'SELECT qtype, COUNT(*) AS n FROM quiz_questions WHERE $where GROUP BY qtype',
      args,
    );
    return {for (final r in rows) r['qtype'] as String: r['n'] as int};
  }

  // 从题库随机抽题组卷。某个题型不够的话有多少抽多少,不报错
  static Future<List<QuizQuestion>> sampleQuiz(
    int bookId,
    Map<String, int> want, {
    List<int>? chapterIds,
  }) async {
    final d = await db;
    final out = <QuizQuestion>[];
    for (final e in want.entries) {
      if (e.value <= 0) continue;
      var where = 'book_id=? AND qtype=?';
      final args = <Object?>[bookId, e.key];
      if (chapterIds != null && chapterIds.isNotEmpty) {
        where += ' AND chapter_id IN (${_inClause(chapterIds)})';
        args.addAll(chapterIds);
      }
      final rows = await d.query(
        'quiz_questions',
        where: where,
        whereArgs: args,
        orderBy: 'RANDOM()',
        limit: e.value,
      );
      out.addAll(rows.map(QuizQuestion.fromMap));
    }
    return out;
  }

  static Future<List<QuizQuestion>> quizByIds(List<int> ids) async {
    if (ids.isEmpty) return const [];
    final rows = await (await db).query(
      'quiz_questions',
      where: 'id IN (${_inClause(ids)})',
      whereArgs: ids,
      orderBy: 'id',
    );
    return rows.map(QuizQuestion.fromMap).toList();
  }

  static Future<QuizQuestion?> quizById(int id) async {
    final rows = await (await db).query(
      'quiz_questions',
      where: 'id=?',
      whereArgs: [id],
      limit: 1,
    );
    return rows.isEmpty ? null : QuizQuestion.fromMap(rows.first);
  }

  static Future<void> deleteQuizQuestion(int id) async {
    await (await db).delete('quiz_questions', where: 'id=?', whereArgs: [id]);
  }

  // ---------- 习题 FSRS 调度 ----------

  static Future<void> applyQuizFsrs(QuizQuestion q, int rating) async {
    final localNow = DateTime.now();
    final dayKey = localDay(localNow);
    final now = localNow.toUtc();
    final card = CardItem(
      id: q.id,
      bookId: q.bookId,
      chapterId: q.chapterId,
      type: 'quiz',
      front: q.stem,
      back: q.answer,
      sourcePage: q.sourcePage,
      status: CardStatus.active,
      stability: q.qStability,
      difficulty: q.qDifficulty,
      state: q.qState,
      step: q.qStep,
      lastReview: q.qLastReview,
      due: q.qDue ?? now,
      reps: q.qReps,
      lapses: q.qLapses,
      firstReviewedDay: q.qFirstReviewedDay,
      createdAt: q.createdAt,
    );
    final result = Fsrs().review(card, rating, now);
    final updated = result.card;
    updated.firstReviewedDay ??= dayKey;
    await (await db).update(
      'quiz_questions',
      {
        'q_stability': updated.stability,
        'q_difficulty': updated.difficulty,
        'q_state': updated.state,
        'q_step': updated.step,
        'q_due': updated.due.toIso8601String(),
        'q_last_review': updated.lastReview!.toIso8601String(),
        'q_reps': updated.reps,
        'q_lapses': updated.lapses,
        'q_first_reviewed_day': updated.firstReviewedDay,
      },
      where: 'id=?',
      whereArgs: [q.id],
    );
  }

  static Future<void> applyQuizResultFsrs(
    QuizQuestion q, {
    bool? correct,
    int? score,
  }) async {
    final current = await quizById(q.id) ?? q;
    final rating = _quizRatingForResult(
      current,
      correct: correct,
      score: score,
    );
    if (rating == null) return;
    await applyQuizFsrs(current, rating);
  }

  static int? _quizRatingForResult(
    QuizQuestion q, {
    bool? correct,
    int? score,
  }) {
    if (correct != null) {
      if (!correct) return Rating.again;
      return q.qState > CardState.newCard ? Rating.good : null;
    }
    if (score == null) return null;
    if (score < 6) return Rating.again;
    return q.qState > CardState.newCard ? Rating.good : null;
  }

  static String _wrongAvailableWhere({String? qtype}) {
    var where =
        'book_id=? AND q_state>=1 AND (q_due<=? OR (q_state IN (1,3) AND q_due<=?))';
    if (qtype != null) where += ' AND qtype=?';
    return where;
  }

  static List<Object?> _wrongAvailableArgs(int bookId, {String? qtype}) {
    final now = nowIso();
    final aheadUntil = DateTime.now()
        .add(const Duration(minutes: learnAheadMinutes))
        .toUtc()
        .toIso8601String();
    return [bookId, now, aheadUntil, ?qtype];
  }

  static Future<QuizQuestion?> nextWrongQuestion(
    int bookId, {
    String? qtype,
  }) async {
    final d = await db;
    final rows = await d.query(
      'quiz_questions',
      where: _wrongAvailableWhere(qtype: qtype),
      whereArgs: _wrongAvailableArgs(bookId, qtype: qtype),
      orderBy: 'q_due',
      limit: 1,
    );
    return rows.isEmpty ? null : QuizQuestion.fromMap(rows.first);
  }

  static Future<int> wrongDueCount(int bookId, {String? qtype}) async {
    final rows = await (await db).rawQuery(
      'SELECT COUNT(*) AS n FROM quiz_questions WHERE ${_wrongAvailableWhere(qtype: qtype)}',
      _wrongAvailableArgs(bookId, qtype: qtype),
    );
    return rows.first['n'] as int;
  }

  static Future<int> wrongTotalCount(int bookId) async {
    final rows = await (await db).rawQuery(
      'SELECT COUNT(*) AS n FROM quiz_questions WHERE book_id=? AND q_state>=1',
      [bookId],
    );
    return rows.first['n'] as int;
  }

  static Future<Map<String, int>> wrongDueCountsByType(int bookId) async {
    final rows = await (await db).rawQuery('''
SELECT qtype, COUNT(*) AS n FROM quiz_questions
WHERE ${_wrongAvailableWhere()}
GROUP BY qtype''', _wrongAvailableArgs(bookId));
    return {for (final r in rows) r['qtype'] as String: r['n'] as int};
  }

  static Future<List<QuizQuestion>> quizReviewQuestions() async {
    final rows = await (await db).rawQuery('''
SELECT q.* FROM quiz_questions q
JOIN books b ON b.id=q.book_id
WHERE b.deleted_at IS NULL AND q.q_state>=1''');
    return rows.map(QuizQuestion.fromMap).toList();
  }

  // ---------- 习题集(答题记录) ----------

  // 记一次作答。客观题传correct,主观题传score,没评分就都是null
  static Future<int> insertQuizAttempt(
    int questionId,
    int bookId, {
    bool? correct,
    int? score,
    String userAnswer = '',
  }) async {
    return (await db).insert('quiz_attempts', {
      'question_id': questionId,
      'book_id': bookId,
      'correct': correct == null ? null : (correct ? 1 : 0),
      'score': score,
      'user_answer': userAnswer,
      'created_at': nowIso(),
    });
  }

  // 习题集页顶上的计数:每个题型做过几道。同一道做多遍只算一道
  static Future<Map<String, int>> attemptedCounts(int bookId) async {
    final rows = await (await db).rawQuery(
      '''
SELECT q.qtype, COUNT(DISTINCT a.question_id) AS n
FROM quiz_attempts a JOIN quiz_questions q ON q.id=a.question_id
WHERE a.book_id=? GROUP BY q.qtype''',
      [bookId],
    );
    return {for (final r in rows) r['qtype'] as String: r['n'] as int};
  }

  // 做过的题列表,带最近一次作答结果(MAX(id)那个子查询就是取每题最新一条),
  // 重练过的以最后一次为准,最近做的排前面
  static Future<List<AttemptedQuestion>> attemptedQuestions(
    int bookId,
    String qtype,
  ) async {
    final rows = await (await db).rawQuery(
      '''
SELECT q.*, a.correct AS a_correct, a.score AS a_score,
       a.user_answer AS a_user_answer, a.created_at AS a_created_at
FROM quiz_questions q
JOIN (SELECT question_id, MAX(id) AS mid FROM quiz_attempts GROUP BY question_id) t
  ON t.question_id = q.id
JOIN quiz_attempts a ON a.id = t.mid
WHERE q.book_id=? AND q.qtype=?
ORDER BY a.id DESC''',
      [bookId, qtype],
    );
    return rows.map(AttemptedQuestion.fromMap).toList();
  }

  // ---------- 问答收藏 ----------

  static Future<void> saveAnswer(
    int bookId,
    String question,
    String answer,
  ) async {
    await (await db).insert('saved_answers', {
      'book_id': bookId,
      'question': question,
      'answer': answer,
      'created_at': nowIso(),
    });
  }

  static Future<List<Map<String, Object?>>> savedAnswers(int bookId) async {
    return (await db).query(
      'saved_answers',
      where: 'book_id=?',
      whereArgs: [bookId],
      orderBy: 'id DESC',
    );
  }

  static Future<int> savedAnswerCount(int bookId) async {
    final rows = await (await db).rawQuery(
      'SELECT COUNT(*) AS n FROM saved_answers WHERE book_id=?',
      [bookId],
    );
    return rows.first['n'] as int;
  }

  static Future<void> deleteSavedAnswer(int id) async {
    await (await db).delete('saved_answers', where: 'id=?', whereArgs: [id]);
  }

  // ---------- 图书问答 ----------

  static Future<List<Map<String, Object?>>> chatHistory(
    int bookId, {
    int limit = 200,
  }) async {
    final rows = await (await db).query(
      'chat_messages',
      where: 'book_id=?',
      whereArgs: [bookId],
      orderBy: 'id DESC',
      limit: limit,
    );
    return rows.reversed.toList();
  }

  static Future<void> insertChatMessage(
    int bookId,
    String role,
    String content,
  ) async {
    await (await db).insert('chat_messages', {
      'book_id': bookId,
      'role': role,
      'content': content,
      'created_at': nowIso(),
    });
  }

  static Future<void> clearChat(int bookId) async {
    await (await db).delete(
      'chat_messages',
      where: 'book_id=?',
      whereArgs: [bookId],
    );
  }
}
