import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:approval_sample/audit_binding.dart';
import 'package:approval_sample/db.dart';
import 'package:approval_sample/models.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

const policies = ['ingest_only', 'versioned_tx', 'rehearsalguard'];
const objectFields = [
  'book_id',
  'chapter_id',
  'type',
  'front',
  'back',
  'source_page',
  'source_evidence',
  'source_context',
  'source_evidence_groups',
  'source_evidence_start',
  'source_evidence_end',
];
const auditFields = [
  'audit_status',
  'audit_quotes',
  'audit_model',
  'audit_reason',
  'audit_schema_version',
  'audit_answer_label',
  'audit_decision',
  'audit_error_probability',
  'audit_expected_reviews',
  'audit_content_hash',
];

class Request {
  final CardItem card;
  final int revision;
  final int ticket;
  Request(this.card, this.revision, [this.ticket = 0]);
}

class Engine {
  final String policy;
  int id = 0;
  int book = 0;
  Engine(this.policy);
  bool get tracked => const [
    'versioned_tx',
    'snapshot_tx',
    'snapshot_epoch',
    'latest_request_tx',
    'value_cas',
  ].contains(policy);

  // A strong value-identity comparator without digest computation.
  static String snapshot(CardItem c) => jsonEncode({
    'book_id': c.bookId,
    'chapter_id': c.chapterId,
    'type': c.type,
    'front': c.front,
    'back': c.back,
    'source_page': c.sourcePage,
    'source_evidence': c.sourceEvidence,
    'source_context': c.sourceContext,
    'source_evidence_groups': c.sourceEvidenceGroups,
    'source_evidence_start': c.sourceEvidenceStart,
    'source_evidence_end': c.sourceEvidenceEnd,
  });

  Future<Map<String, Object?>> row() async => (await (await Db.db).query(
    'cards',
    where: 'id=?',
    whereArgs: [id],
  )).single;

  Future<void> seed() async {
    await Db.reset();
    book = await Db.insertBook('synthetic fixture', 'fixture.pdf', 2, false);
    await Db.insertDraftCards(book, null, [
      DraftCard(
        type: 'qa',
        front: 'Which token?',
        back: 'Alpha.',
        page: 1,
        evidenceQuote: 'Alpha.',
        sourceContext: 'One block contains Alpha.',
        evidenceGroups: const [
          'One block contains Alpha.',
          'Second block contains Beta.',
        ],
      ),
    ]);
    id = (await Db.pendingCards(book)).single.id;
    if (tracked) {
      final d = await Db.db;
      await d.execute(
        'CREATE TABLE revisions(card_id INTEGER PRIMARY KEY, obj INTEGER NOT NULL, audit INTEGER NOT NULL)',
      );
      await d.insert('revisions', {'card_id': id, 'obj': 0, 'audit': 0});
      await d.execute(
        'CREATE TABLE approvals(card_id INTEGER PRIMARY KEY, obj INTEGER NOT NULL, audit INTEGER NOT NULL, snapshot TEXT NOT NULL, ticket INTEGER NOT NULL DEFAULT 0, authority_epoch INTEGER NOT NULL DEFAULT 0)',
      );
      if (policy == 'latest_request_tx') {
        await d.execute(
          'CREATE TABLE requests(card_id INTEGER PRIMARY KEY, ticket INTEGER NOT NULL)',
        );
        await d.insert('requests', {'card_id': id, 'ticket': 0});
      }
      for (final part in ['obj', 'audit']) {
        final fields = part == 'obj' ? objectFields : auditFields;
        final changed = fields.map((f) => 'OLD.$f IS NOT NEW.$f').join(' OR ');
        await d.execute(
          'CREATE TRIGGER bump_$part AFTER UPDATE OF ${fields.join(',')} ON cards '
          'WHEN $changed BEGIN UPDATE revisions SET $part=$part+1 WHERE card_id=NEW.id; END',
        );
      }
    }
  }

  Future<Request> capture() async {
    if (tracked) {
      return (await Db.db).transaction((tx) async {
        final current = (await tx.query(
          'cards',
          where: 'id=?',
          whereArgs: [id],
        )).single;
        final revision =
            (await tx.query(
                  'revisions',
                  where: 'card_id=?',
                  whereArgs: [id],
                )).single['obj']
                as int;
        var ticket = 0;
        if (policy == 'latest_request_tx') {
          // This policy starts approval requests only while pending. A new
          // request revokes any pending approval from an earlier request.
          if (current['status'] != CardStatus.pending) {
            return Request(CardItem.fromMap(current), revision, -1);
          }
          await tx.rawUpdate(
            'UPDATE requests SET ticket=ticket+1 WHERE card_id=?',
            [id],
          );
          ticket =
              (await tx.query(
                    'requests',
                    where: 'card_id=?',
                    whereArgs: [id],
                  )).single['ticket']
                  as int;
          await tx.update(
            'cards',
            {'audit_status': AuditStatus.stale},
            where: 'id=?',
            whereArgs: [id],
          );
        }
        return Request(CardItem.fromMap(current), revision, ticket);
      });
    }
    return Request(CardItem.fromMap(await row()), 0);
  }

  Future<bool> commit(
    Request request, {
    List<String> quotes = const ['Alpha.'],
    String? label,
    String? decision,
    String model = 'deterministic-fixture',
    String reason = 'programmed positive response',
    String auditStatus = AuditStatus.passed,
    Map<String, Object?> metadata = const {},
  }) async {
    label ??= CardAuditContract.supported;
    decision ??= CardAuditContract.pass;
    if (policy == 'rehearsalguard') {
      return Db.updateCardAudit(
        id: id,
        auditStatus: auditStatus,
        errorProbability: auditStatus == AuditStatus.passed ? 0 : 1,
        expectedReviews: 1,
        evidenceQuotes: quotes,
        model: model,
        reason: reason,
        expectedContentHash: cardAuditContentHash(request.card),
        expectedApprovalEpoch: request.card.approvalEpoch,
        schemaVersion: CardAuditContract.schemaVersion,
        answerLabel: label,
        decision: decision,
        metadata: metadata,
      );
    }
    // Each baseline validates its declared version binding.
    // Intake validation covers the same canonical positive response and quotes.
    final input = request.card;
    final groups = input.sourceEvidenceGroups.isNotEmpty
        ? input.sourceEvidenceGroups
        : [
            input.sourceContext.isNotEmpty
                ? input.sourceContext
                : input.sourceEvidence,
          ];
    if (auditStatus == AuditStatus.passed &&
        (label != CardAuditContract.supported ||
            decision != CardAuditContract.pass ||
            model.trim().isEmpty ||
            reason.trim().isEmpty ||
            quotes.isEmpty ||
            quotes.any(
              (q) => q.trim().isEmpty || !groups.any((g) => g.contains(q)),
            ))) {
      return false;
    }
    return (await Db.db).transaction((tx) async {
      final current = (await tx.query(
        'cards',
        where: 'id=?',
        whereArgs: [id],
      )).single;
      if (current['status'] != CardStatus.pending) return false;
      if (policy == 'snapshot_epoch' &&
          current['approval_epoch'] != request.card.approvalEpoch) {
        return false;
      }
      if (policy == 'versioned_tx' || policy == 'latest_request_tx') {
        final rev = (await tx.query(
          'revisions',
          where: 'card_id=?',
          whereArgs: [id],
        )).single;
        if (rev['obj'] != request.revision) return false;
      }
      if (policy == 'latest_request_tx') {
        final latest = (await tx.query(
          'requests',
          where: 'card_id=?',
          whereArgs: [id],
        )).single['ticket'];
        if (request.ticket < 0 || latest != request.ticket) return false;
      }
      if ((policy == 'snapshot_tx' ||
              policy == 'snapshot_epoch' ||
              policy == 'value_cas') &&
          snapshot(CardItem.fromMap(current)) != snapshot(request.card)) {
        return false;
      }
      await tx.update(
        'cards',
        {
          'audit_status': auditStatus,
          'audit_error_probability': auditStatus == AuditStatus.passed
              ? 0.0
              : 1.0,
          'audit_expected_reviews': 1.0,
          'audit_quotes': jsonEncode(quotes),
          'audit_model': model,
          'audit_reason': reason,
          'audit_schema_version': CardAuditContract.schemaVersion,
          'audit_answer_label': label,
          'audit_decision': decision,
          'audit_metadata': jsonEncode(metadata),
          'audit_approval_epoch': current['approval_epoch'],
        },
        where: 'id=?',
        whereArgs: [id],
      );
      if (tracked) {
        final rev = (await tx.query(
          'revisions',
          where: 'card_id=?',
          whereArgs: [id],
        )).single;
        await tx.insert('approvals', {
          ...rev,
          'snapshot': snapshot(request.card),
          'ticket': request.ticket,
          'authority_epoch': current['approval_epoch'],
        }, conflictAlgorithm: ConflictAlgorithm.replace);
      }
      return true;
    });
  }

  Future<bool> activate(String entry) async {
    if (policy == 'rehearsalguard') {
      if (entry == 'single') return Db.updateCardStatus(id, CardStatus.active);
      final current = await row();
      return await Db.approvePending(current['book_id'] as int) == 1;
    }
    if (policy == 'value_cas') return activateValueCas(entry);
    return (await Db.db).transaction((tx) async {
      final current = (await tx.query(
        'cards',
        where: 'id=?',
        whereArgs: [id],
      )).single;
      final status = current['status'];
      if (status != CardStatus.pending &&
          !(entry == 'single' && status == CardStatus.suspended)) {
        return false;
      }
      if (current['audit_status'] != AuditStatus.passed) return false;
      if (tracked) {
        final approvals = await tx.query(
          'approvals',
          where: 'card_id=?',
          whereArgs: [id],
        );
        if (approvals.isEmpty) return false;
        if (policy == 'snapshot_epoch' &&
            approvals.single['authority_epoch'] != current['approval_epoch']) {
          return false;
        }
        final rev = (await tx.query(
          'revisions',
          where: 'card_id=?',
          whereArgs: [id],
        )).single;
        if (approvals.single['audit'] != rev['audit']) {
          return false;
        }
        if ((policy == 'versioned_tx' || policy == 'latest_request_tx') &&
            approvals.single['obj'] != rev['obj']) {
          return false;
        }
        if (policy == 'latest_request_tx') {
          final latest = (await tx.query(
            'requests',
            where: 'card_id=?',
            whereArgs: [id],
          )).single['ticket'];
          if (approvals.single['ticket'] != latest) return false;
        }
        if ((policy == 'snapshot_tx' || policy == 'snapshot_epoch') &&
            approvals.single['snapshot'] !=
                snapshot(CardItem.fromMap(current))) {
          return false;
        }
      }
      return await tx.update(
            'cards',
            {'status': CardStatus.active},
            where: 'id=? AND status=?',
            whereArgs: [id, status],
          ) ==
          1;
    });
  }

  Future<bool> activateValueCas(String entry) async {
    final d = await Db.db;
    final current = await row();
    final status = current['status'];
    if (status != CardStatus.pending &&
        !(entry == 'single' && status == CardStatus.suspended)) {
      return false;
    }
    if (current['audit_status'] != AuditStatus.passed) return false;
    final approvals = await d.query(
      'approvals',
      where: 'card_id=?',
      whereArgs: [id],
    );
    if (approvals.isEmpty ||
        approvals.single['snapshot'] != snapshot(CardItem.fromMap(current))) {
      return false;
    }
    final approval = approvals.single;
    // Reading and validation can interleave with writers. The SQL predicate
    // atomically checks every observed bound/audit field and audit revision.
    final fields = {...objectFields, ...auditFields}.toList();
    final where =
        'id=? AND status=? AND ${fields.map((f) => current[f] == null ? "$f IS NULL" : "$f IS ?").join(" AND ")} '
        'AND EXISTS (SELECT 1 FROM revisions r WHERE r.card_id=cards.id AND r.audit=?)';
    final changed = await d.update(
      'cards',
      {'status': CardStatus.active},
      where: where,
      whereArgs: [
        id,
        status,
        for (final field in fields)
          if (current[field] != null) current[field],
        approval['audit'],
      ],
    );
    return changed == 1;
  }

  Future<void> inject(Map<String, Object?> values) async {
    await (await Db.db).update('cards', values, where: 'id=?', whereArgs: [id]);
  }

  Future<void> edit(String answer) =>
      Db.updateCardText(id, 'Which token?', answer);
}

class Scenario {
  final String name;
  final String group;
  final bool expected;
  final Future<void> Function(Engine) prepare;
  final List<String> entries;
  Scenario(
    this.name,
    this.group,
    this.expected,
    this.prepare, {
    this.entries = const ['single', 'batch'],
  });
}

List<Scenario> scenarios() {
  Future<void> positive(Engine e) async {
    expect(await e.commit(await e.capture()), isTrue);
  }

  final cases = <Scenario>[
    Scenario('no_audit', 'normal_lifecycle', false, (e) async {}),
    Scenario('valid_pending', 'normal_lifecycle', true, positive),
    Scenario('negative_decision', 'normal_lifecycle', false, (e) async {
      expect(
        await e.commit(await e.capture(), decision: CardAuditContract.reject),
        isFalse,
      );
    }),
    Scenario('invalid_quote', 'normal_lifecycle', false, (e) async {
      expect(
        await e.commit(await e.capture(), quotes: ['Not present']),
        isFalse,
      );
    }),
    Scenario('cross_block_quote', 'normal_lifecycle', false, (e) async {
      expect(
        await e.commit(await e.capture(), quotes: ['Alpha.\n\nBeta']),
        isFalse,
      );
    }),
    Scenario('empty_model', 'normal_lifecycle', false, (e) async {
      expect(await e.commit(await e.capture(), model: ''), isFalse);
    }),
    Scenario('edit_after_pass', 'normal_lifecycle', false, (e) async {
      await positive(e);
      await e.edit('Changed');
    }),
    Scenario('delayed_response_after_edit', 'normal_lifecycle', false, (
      e,
    ) async {
      final request = await e.capture();
      await e.edit('Changed');
      await e.commit(request);
    }),
    Scenario('edit_then_reaudit', 'normal_lifecycle', true, (e) async {
      await positive(e);
      await e.edit('Alpha.');
      await positive(e);
    }),
    Scenario('active_edit_removes_eligibility', 'normal_lifecycle', false, (
      e,
    ) async {
      await positive(e);
      expect(await e.activate('single'), isTrue);
      await e.edit('Changed');
    }),
    Scenario('suspended_valid_restore', 'normal_lifecycle', true, (e) async {
      await positive(e);
      expect(await e.activate('single'), isTrue);
      expect(await Db.updateCardStatus(e.id, CardStatus.suspended), isTrue);
    }, entries: ['single']),
    Scenario('suspended_edit_then_restore', 'normal_lifecycle', false, (
      e,
    ) async {
      await positive(e);
      expect(await e.activate('single'), isTrue);
      expect(await Db.updateCardStatus(e.id, CardStatus.suspended), isTrue);
      await e.edit('Changed');
    }),
    Scenario(
      'response_arrives_when_suspended',
      'normal_lifecycle',
      false,
      (e) async {
        final request = await e.capture();
        expect(await Db.updateCardStatus(e.id, CardStatus.suspended), isTrue);
        expect(await e.commit(request), isFalse);
      },
      entries: ['single'],
    ),
    Scenario('nonbinding_schedule_update', 'normal_lifecycle', true, (e) async {
      await positive(e);
      await e.inject({'reps': 2, 'due': '2020-01-01T00:00:00Z'});
    }),
  ];
  final objectMutations = <String, Object?>{
    'type': 'cloze',
    'front': 'A different question',
    'back': 'A different answer',
    'source_page': 2,
    'source_evidence': 'A different anchor',
    'source_context': 'A different context',
    'source_evidence_groups': jsonEncode(['Another block containing Alpha.']),
    'source_evidence_start': 5,
    'source_evidence_end': 11,
  };
  for (final entry in objectMutations.entries) {
    cases.add(
      Scenario('mutate_${entry.key}', 'injected_core', false, (e) async {
        await positive(e);
        await e.inject({entry.key: entry.value});
      }),
    );
  }
  final auditMutations = <String, Object?>{
    'audit_status': 'unknown_audit_status',
    'audit_schema_version': 'unexpected_schema',
    'audit_answer_label': 'UNSUPPORTED',
    'audit_decision': 'REJECT',
    'audit_quotes': '["not in evidence"]',
    'audit_model': '',
    'audit_reason': '',
    'status': 'unknown_lifecycle_state',
  };
  for (final entry in auditMutations.entries) {
    cases.add(
      Scenario('mutate_${entry.key}', 'injected_core', false, (e) async {
        await positive(e);
        await e.inject({entry.key: entry.value});
      }),
    );
  }
  cases.add(
    Scenario('change_book_identity', 'expanded_claim', false, (e) async {
      await positive(e);
      final other = await Db.insertBook('other book', 'other.pdf', 2, false);
      await e.inject({'book_id': other});
    }),
  );
  cases.add(
    Scenario('change_chapter_identity', 'expanded_claim', false, (e) async {
      await positive(e);
      final chapter = await (await Db.db).insert('chapters', {
        'book_id': e.book,
        'idx': 1,
        'title': 'Another chapter',
        'page_start': 1,
        'page_end': 2,
        'char_count': 1,
      });
      await e.inject({'chapter_id': chapter});
    }),
  );
  for (final entry in <String, Object?>{
    'audit_error_probability': 1.5,
    'audit_expected_reviews': -1.0,
  }.entries) {
    cases.add(
      Scenario('invalid_${entry.key}', 'expanded_claim', false, (e) async {
        await positive(e);
        await e.inject({entry.key: entry.value});
      }),
    );
  }
  return cases;
}

Future<Map<String, Object?>> benchmark() async {
  final values = <String, List<int>>{for (final p in policies) p: []};
  final random = Random(20260929);
  for (var round = 0; round < 5; round++) {
    final order = [...policies]..shuffle(random);
    for (final policy in order) {
      final engine = Engine(policy);
      await engine.seed();
      expect(await engine.commit(await engine.capture()), isTrue);
      for (var i = 0; i < 120; i++) {
        await engine.inject({'status': CardStatus.pending});
        final watch = Stopwatch()..start();
        final released = await engine.activate('single');
        watch.stop();
        expect(released, isTrue);
        if (i >= 20) values[policy]!.add(watch.elapsedMicroseconds);
      }
    }
  }
  final result = <String, Object?>{};
  for (final entry in values.entries) {
    final xs = entry.value..sort();
    result[entry.key] = {
      'n': xs.length,
      'median_us': xs[xs.length ~/ 2],
      'p95_us': xs[(xs.length * .95).ceil() - 1],
      'min_us': xs.first,
      'max_us': xs.last,
    };
  }
  return result;
}

void main() {
  setUpAll(() {
    sqfliteFfiInit();
    Db.factoryOverride = databaseFactoryFfi;
    Db.pathOverride = inMemoryDatabasePath;
  });
  tearDownAll(() async => Db.reset());

  test(
    'paired lifecycle comparison on the real frozen SQLite application',
    () async {
      final cases = scenarios();
      final results = <Map<String, Object?>>[];
      for (final scenario in cases) {
        for (final entry in scenario.entries) {
          for (final policy in policies) {
            final engine = Engine(policy);
            await engine.seed();
            await scenario.prepare(engine);
            final before = await engine.row();
            final released = await engine.activate(entry);
            final after = await engine.row();
            // Check observed transition independently of the API return value.
            expect(
              after['status'] == CardStatus.active,
              released,
              reason:
                  '${scenario.name}/$entry/$policy inconsistent API and stored state',
            );
            results.add({
              'case_id': scenario.name,
              'group': scenario.group,
              'entry': entry,
              'policy': policy,
              'expected_release': scenario.expected,
              'released': released,
              'status_before': before['status'],
              'status_after': after['status'],
              'contract_violation': released && !scenario.expected,
              'valid_release_blocked': !released && scenario.expected,
            });
          }
        }
      }
      final summary = <String, Object?>{};
      for (final group in [
        'normal_lifecycle',
        'injected_core',
        'expanded_claim',
      ]) {
        summary[group] = {
          for (final policy in policies)
            policy: () {
              final rows = results
                  .where((r) => r['group'] == group && r['policy'] == policy)
                  .toList();
              return {
                'opportunities': rows.length,
                'expected_valid': rows
                    .where((r) => r['expected_release'] == true)
                    .length,
                'released': rows.where((r) => r['released'] == true).length,
                'violations': rows
                    .where((r) => r['contract_violation'] == true)
                    .length,
                'valid_blocked': rows
                    .where((r) => r['valid_release_blocked'] == true)
                    .length,
              };
            }(),
        };
      }
      final matched = results.where((r) => r['policy'] == 'rehearsalguard').map(
        (rg) {
          final baseline = results.singleWhere(
            (r) =>
                r['policy'] == 'versioned_tx' &&
                r['case_id'] == rg['case_id'] &&
                r['entry'] == rg['entry'],
          );
          return <String, Object?>{
            'case_id': rg['case_id'],
            'group': rg['group'],
            'entry': rg['entry'],
            'rehearsalguard_release': rg['released'],
            'baseline_release': baseline['released'],
            'different': rg['released'] != baseline['released'],
          };
        },
      ).toList();
      final timing = await benchmark();
      const output = String.fromEnvironment('LIFECYCLE_REPORT');
      expect(output, isNotEmpty);
      final report = {
        'schema': 'rehearsalguard.lifecycle_comparison.20260929.v1',
        'scope':
            'deterministic feasibility probes on in-memory SQLite',
        'human_annotations_added': 0,
        'hosted_model_calls': 0,
        'scenario_count': cases.length,
        'policy_runs': results.length,
        'summary': summary,
        'paired': matched,
        'rows': results,
        'activation_timing_exploratory': timing,
        'timing_scope':
            'single-card successful activation; in-memory SQLite; 5 shuffled rounds; 20 warmups + 100 samples per policy per round; reset outside timer',
      };
      File(output).parent.createSync(recursive: true);
      File(output).writeAsStringSync(
        '${const JsonEncoder.withIndent('  ').convert(report)}\n',
      );
      // Retain every design outcome and its independently checked findings.
      expect(results.length, 204);
      for (final row in results.where((r) => r['policy'] == 'versioned_tx')) {
        expect(
          row['released'],
          row['expected_release'],
          reason: 'strong baseline correctness: ${row['case_id']}',
        );
      }
      stdout.writeln(
        jsonEncode({'summary': summary, 'timing': timing, 'report': output}),
      );
    },
    timeout: const Timeout(Duration(minutes: 3)),
  );
}
