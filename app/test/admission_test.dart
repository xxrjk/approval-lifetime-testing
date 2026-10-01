import 'dart:convert';
import 'dart:io';
import 'dart:math';

import 'package:flutter_test/flutter_test.dart';
import 'package:approval_sample/db.dart';
import 'package:approval_sample/models.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';

import 'lifecycle_test.dart' as fixtures;

const methods = [
  'ingest_only',
  'versioned_tx',
  'snapshot_tx',
  'rehearsalguard',
  'latest_request_tx',
  'value_cas',
];

// Independent specification: compare named values, not RG's digest or gate.
Map<String, Object?> binding(Map<String, Object?> row) => {
  for (final name in [
    'book_id',
    'chapter_id',
    'type',
    'front',
    'back',
    'source_page',
    'source_evidence',
    'source_context',
    'source_evidence_start',
    'source_evidence_end',
  ])
    name: row[name],
  'evidence_blocks': jsonDecode(row['source_evidence_groups'] as String),
};

bool sameValues(Map<String, Object?> a, Map<String, Object?> b) =>
    a.keys.length == b.keys.length &&
    a.entries.every(
      (e) =>
          b.containsKey(e.key) && jsonEncode(e.value) == jsonEncode(b[e.key]),
    );

class Packet {
  final String id;
  final fixtures.Request request;
  final Map<String, Object?> object;
  Packet(this.id, this.request, this.object);
}

bool releaseOracle(
  Map<String, Object?> row,
  Map<String, Packet> packets, {
  String entry = 'single',
  bool requirePending = true,
  String? requiredPacketId,
}) {
  final allowed = requirePending
      ? row['status'] == 'pending' ||
            (entry == 'single' && row['status'] == 'suspended')
      : row['status'] == 'active';
  if (!allowed ||
      row['audit_status'] != 'passed' ||
      row['audit_schema_version'] != CardAuditContract.schemaVersion ||
      row['audit_answer_label'] != 'SUPPORTED' ||
      row['audit_decision'] != 'PASS') {
    return false;
  }
  final metadata = jsonDecode(row['audit_metadata'] as String) as Map;
  final packet = packets[metadata['packet_id']];
  if (packet == null || !sameValues(packet.object, binding(row))) return false;
  if (requiredPacketId != null && packet.id != requiredPacketId) return false;
  final probability = row['audit_error_probability'] as num?;
  final reviews = row['audit_expected_reviews'] as num;
  if (probability != null &&
      (!probability.isFinite || probability < 0 || probability > 1)) {
    return false;
  }
  if (!reviews.isFinite || reviews < 0) return false;
  if ((row['audit_model'] as String? ?? '').trim().isEmpty ||
      (row['audit_reason'] as String? ?? '').trim().isEmpty) {
    return false;
  }
  final quotes = jsonDecode(row['audit_quotes'] as String) as List;
  final groups = jsonDecode(row['source_evidence_groups'] as String) as List;
  return quotes.isNotEmpty &&
      quotes.every(
        (q) =>
            q is String &&
            q.trim().isNotEmpty &&
            groups.any((g) => g is String && g.contains(q)),
      );
}

class Trace {
  final String family;
  final List<String> events;
  final bool rejectB;
  Trace(this.family, this.events, {this.rejectB = false});
}

Iterable<List<String>> interleave(List<List<String>> chains) sync* {
  if (chains.every((c) => c.isEmpty)) {
    yield [];
    return;
  }
  for (var i = 0; i < chains.length; i++) {
    if (chains[i].isEmpty) continue;
    final rest = [
      for (var j = 0; j < chains.length; j++)
        j == i ? chains[j].sublist(1) : chains[j],
    ];
    for (final tail in interleave(rest)) {
      yield [chains[i].first, ...tail];
    }
  }
}

List<Trace> makeTraces() {
  final traces = <Trace>[];
  void add(
    String family,
    List<List<String>> chains, {
    List<String> prefix = const [],
    List<String> suffix = const [],
    bool rejectB = false,
  }) {
    for (final events in interleave(chains)) {
      traces.add(
        Trace(family, [...prefix, ...events, ...suffix], rejectB: rejectB),
      );
    }
  }

  add('single_edit', [
    ['request_a', 'complete_a', 'activate'],
    ['edit_b'],
  ]);
  add('content_restoration', [
    ['request_a', 'complete_a', 'activate'],
    ['edit_b', 'edit_a'],
  ]);
  add('repeated_edit', [
    ['request_a', 'complete_a', 'activate'],
    ['edit_b', 'edit_a', 'edit_b'],
  ]);
  add(
    'suspend_and_reaudit',
    [
      ['activate', 'suspend', 'activate'],
      ['edit_b', 'request_b', 'complete_b'],
    ],
    prefix: ['request_a', 'complete_a'],
  );
  for (final reject in [false, true]) {
    add(
      reject ? 'two_responses_mixed' : 'two_responses_positive',
      [
        ['request_a', 'complete_a'],
        ['request_b', 'complete_b'],
        ['edit_b', 'edit_a'],
      ],
      suffix: ['activate'],
      rejectB: reject,
    );
  }
  const seed = int.fromEnvironment('TRACE_SEED', defaultValue: 20260929);
  const holdout = bool.fromEnvironment('HOLDOUT', defaultValue: false);
  const count = int.fromEnvironment('LONG_TRACE_COUNT', defaultValue: 256);
  const randomSteps = int.fromEnvironment('RANDOM_STEPS', defaultValue: 10);
  if (holdout) traces.clear();
  final random = Random(seed);
  const alphabet = [
    'request_a',
    'request_b',
    'complete_a',
    'complete_b',
    'edit_a',
    'edit_b',
    'activate',
    'activate_batch',
    'suspend',
  ];
  for (var i = 0; i < count; i++) {
    traces.add(
      Trace('seeded_long', [
        'request_a',
        for (var j = 0; j < randomSteps; j++)
          alphabet[random.nextInt(alphabet.length)],
        'activate',
      ], rejectB: i.isOdd),
    );
  }
  // Exact duplicates do not create additional experimental units.
  final seen = <String>{};
  return traces
      .where((t) => seen.add('${t.family}|${t.rejectB}|${t.events.join(',')}'))
      .toList();
}

Future<Map<String, Object?>> executeTrace(
  int traceId,
  Trace trace,
  String method,
  List<Map<String, Object?>> ledger,
) async {
  final engine = fixtures.Engine(method);
  await engine.seed();
  final pending = <String, Packet>{};
  final packets = <String, Packet>{};
  var serial = 0;
  String? latestPendingPacket;
  bool oracle(
    Map<String, Object?> row, {
    String entry = 'single',
    bool requirePending = true,
  }) => releaseOracle(
    row,
    packets,
    entry: entry,
    requirePending: requirePending,
    requiredPacketId: method == 'latest_request_tx'
        ? latestPendingPacket
        : null,
  );
  final counts = <String, int>{
    'steps': 0,
    'activation_attempts': 0,
    'valid_activation_opportunities': 0,
    'released': 0,
    'invalid_releases': 0,
    'valid_activation_blocked': 0,
    'completion_attempts': 0,
    'content_valid_completions': 0,
    'content_valid_completion_blocked': 0,
    'stale_completion_applied': 0,
    'eligible_approval_clobbered': 0,
    'active_contract_violations': 0,
  };
  for (var step = 0; step < trace.events.length; step++) {
    final event = trace.events[step];
    final before = await engine.row();
    final record = <String, Object?>{
      'trace_id': traceId,
      'family': trace.family,
      'reject_b': trace.rejectB,
      'method': method,
      'step': step,
      'event': event,
      'status_before': before['status'],
      'answer_before': before['back'],
    };
    if (event.startsWith('request_')) {
      final channel = event.substring(event.length - 1);
      final request = await engine.capture();
      final p = Packet(
        '$traceId:${serial++}',
        request,
        binding(await engine.row()),
      );
      packets[p.id] = p;
      pending[channel] = p;
      if (before['status'] == 'pending') latestPendingPacket = p.id;
    } else if (event.startsWith('complete_')) {
      final channel = event.substring(event.length - 1);
      final packet = pending[channel];
      if (packet != null) {
        final positive = !(channel == 'b' && trace.rejectB);
        final valid =
            before['status'] == 'pending' &&
            sameValues(packet.object, binding(before));
        final eligibleBefore = oracle(before);
        final applied = await engine.commit(
          packet.request,
          auditStatus: positive ? AuditStatus.passed : AuditStatus.needsReview,
          label: positive ? CardAuditContract.supported : 'UNSUPPORTED',
          decision: positive
              ? CardAuditContract.pass
              : CardAuditContract.reject,
          quotes: positive ? [packet.request.card.back] : [],
          metadata: {'packet_id': packet.id},
        );
        final after = await engine.row();
        final clobbered = !applied && eligibleBefore && !oracle(after);
        counts['completion_attempts'] = counts['completion_attempts']! + 1;
        if (valid) {
          counts['content_valid_completions'] =
              counts['content_valid_completions']! + 1;
        }
        if (valid && !applied) {
          counts['content_valid_completion_blocked'] =
              counts['content_valid_completion_blocked']! + 1;
        }
        if (!valid && applied) {
          counts['stale_completion_applied'] =
              counts['stale_completion_applied']! + 1;
        }
        if (clobbered) {
          counts['eligible_approval_clobbered'] =
              counts['eligible_approval_clobbered']! + 1;
        }
        record.addAll({
          'packet_id': packet.id,
          'content_valid': valid,
          'applied': applied,
          'content_valid_completion_blocked': valid && !applied,
          'stale_completion_applied': !valid && applied,
          'eligible_approval_clobbered': clobbered,
        });
      } else {
        record['no_outstanding_packet'] = true;
      }
    } else if (event == 'edit_a' || event == 'edit_b') {
      await engine.edit(event == 'edit_a' ? 'Alpha.' : 'Beta.');
    } else if (event == 'suspend') {
      record['applied'] = await Db.updateCardStatus(
        engine.id,
        CardStatus.suspended,
      );
    } else if (event.startsWith('activate')) {
      final entry = event == 'activate_batch' ? 'batch' : 'single';
      final expected = oracle(before, entry: entry);
      final released = await engine.activate(entry);
      counts['activation_attempts'] = counts['activation_attempts']! + 1;
      if (expected) {
        counts['valid_activation_opportunities'] =
            counts['valid_activation_opportunities']! + 1;
      }
      if (released) counts['released'] = counts['released']! + 1;
      if (released && !expected) {
        counts['invalid_releases'] = counts['invalid_releases']! + 1;
      }
      if (!released && expected) {
        counts['valid_activation_blocked'] =
            counts['valid_activation_blocked']! + 1;
      }
      record.addAll({
        'expected_release': expected,
        'released': released,
        'invalid_release': released && !expected,
        'valid_activation_blocked': expected && !released,
      });
    }
    final after = await engine.row();
    final activeViolation =
        after['status'] == 'active' && !oracle(after, requirePending: false);
    if (activeViolation) {
      counts['active_contract_violations'] =
          counts['active_contract_violations']! + 1;
    }
    record.addAll({
      'status_after': after['status'],
      'answer_after': after['back'],
      'audit_status_after': after['audit_status'],
      'active_contract_violation': activeViolation,
    });
    counts['steps'] = counts['steps']! + 1;
    ledger.add(record);
  }
  return {
    'trace_id': traceId,
    'family': trace.family,
    'events': trace.events,
    'reject_b': trace.rejectB,
    'method': method,
    ...counts,
  };
}

Future<Map<String, Object?>> concurrentProbes() async {
  final rows = <Map<String, Object?>>[];
  for (final method in methods) {
    for (final kind in ['approve_edit', 'audit_edit', 'double_activation']) {
      for (var order = 0; order < 2; order++) {
        for (var repetition = 0; repetition < 20; repetition++) {
          final engine = fixtures.Engine(method);
          await engine.seed();
          final request = await engine.capture();
          final packet = Packet(
            'concurrent',
            request,
            binding(await engine.row()),
          );
          final packets = {packet.id: packet};
          if (kind != 'audit_edit') {
            expect(
              await engine.commit(request, metadata: {'packet_id': packet.id}),
              isTrue,
            );
          }
          final operations = <Future<Object?> Function()>[];
          if (kind == 'approve_edit') {
            operations.add(() => engine.activate('single'));
            operations.add(() async {
              await engine.edit('Beta.');
              return null;
            });
          } else if (kind == 'audit_edit') {
            operations.add(
              () => engine.commit(request, metadata: {'packet_id': packet.id}),
            );
            operations.add(() async {
              await engine.edit('Beta.');
              return null;
            });
          } else {
            operations.add(() => engine.activate('single'));
            operations.add(() => engine.activate('batch'));
          }
          final futures = (order == 0 ? operations : operations.reversed)
              .map((f) => f())
              .toList();
          final completed = await Future.wait(futures);
          final after = await engine.row();
          final invalidActive =
              after['status'] == 'active' &&
              !releaseOracle(after, packets, requirePending: false);
          final exactlyOne =
              kind != 'double_activation' ||
              completed.where((v) => v == true).length == 1;
          final editedPending =
              kind == 'double_activation' || after['status'] == 'pending';
          rows.add({
            'method': method,
            'kind': kind,
            'invocation_order': order,
            'repetition': repetition,
            'completed': completed,
            'status_after': after['status'],
            'invalid_active': invalidActive,
            'exactly_one_activation': exactlyOne,
            'edited_object_pending': editedPending,
          });
        }
      }
    }
  }
  return {
    'scope':
        'actual asynchronous application APIs; single sqflite connection queue',
    'trials': rows.length,
    'failures': rows
        .where(
          (r) =>
              r['invalid_active'] == true ||
              r['exactly_one_activation'] != true ||
              r['edited_object_pending'] != true,
        )
        .length,
    'rows': rows,
  };
}

void main() {
  setUpAll(() {
    sqfliteFfiInit();
    Db.factoryOverride = databaseFactoryFfi;
    Db.pathOverride = inMemoryDatabasePath;
  });
  tearDownAll(() async => Db.reset());
  test(
    'exhaustive ordered shuffles and seeded lifecycle histories',
    () async {
      const output = String.fromEnvironment('STATEFUL_REPORT');
      expect(output, isNotEmpty);
      final traces = makeTraces();
      final ledger = <Map<String, Object?>>[];
      final rows = <Map<String, Object?>>[];
      for (var i = 0; i < traces.length; i++) {
        for (final method in methods) {
          rows.add(await executeTrace(i, traces[i], method, ledger));
        }
      }
      final summary = <String, Map<String, int>>{};
      for (final method in methods) {
        final selected = rows.where((r) => r['method'] == method);
        final totals = <String, int>{'histories': selected.length};
        for (final row in selected) {
          for (final e in row.entries.where(
            (e) => e.value is int && e.key != 'trace_id',
          )) {
            totals[e.key] = (totals[e.key] ?? 0) + (e.value as int);
          }
        }
        summary[method] = totals;
      }
      final concurrency = await concurrentProbes();
      final counterexamples = ledger
          .where(
            (r) =>
                r['invalid_release'] == true ||
                r['eligible_approval_clobbered'] == true ||
                r['content_valid_completion_blocked'] == true,
          )
          .toList();
      final result = {
        'protocol': 'rg.fse.stateful.v1',
        'seed': const int.fromEnvironment('TRACE_SEED', defaultValue: 20260929),
        'confirmation': const bool.fromEnvironment(
          'HOLDOUT',
          defaultValue: false,
        ),
        'unique_histories': traces.length,
        'summary': summary,
        'trace_results': rows,
        'counterexamples': counterexamples,
        'concurrency': concurrency,
        'new_human_annotations': 0,
        'hosted_calls': 0,
      };
      final file = File(output);
      file.parent.createSync(recursive: true);
      file.writeAsStringSync(
        '${const JsonEncoder.withIndent('  ').convert(result)}\n',
      );
      File(
        '$output.steps.jsonl',
      ).writeAsStringSync('${ledger.map(jsonEncode).join('\n')}\n');
      stdout.writeln(
        jsonEncode({
          'unique_histories': traces.length,
          'summary': summary,
          'concurrency_trials': concurrency['trials'],
          'concurrency_failures': concurrency['failures'],
        }),
      );
      // Adverse results are reported, not deleted by a success-only assertion.
      for (final method in methods.where((m) => m != 'ingest_only')) {
        expect(summary[method]!['invalid_releases'], 0);
        expect(summary[method]!['active_contract_violations'], 0);
      }
      expect(concurrency['failures'], 0);
      if (const bool.fromEnvironment('HOLDOUT', defaultValue: false)) {
        expect(summary['rehearsalguard']!['eligible_approval_clobbered'], 0);
        for (var i = 0; i < traces.length; i++) {
          final rg = rows.singleWhere(
            (r) => r['trace_id'] == i && r['method'] == 'rehearsalguard',
          );
          final snapshot = rows.singleWhere(
            (r) => r['trace_id'] == i && r['method'] == 'snapshot_tx',
          );
          final cas = rows.singleWhere(
            (r) => r['trace_id'] == i && r['method'] == 'value_cas',
          );
          for (final key in [
            'released',
            'invalid_releases',
            'content_valid_completion_blocked',
            'eligible_approval_clobbered',
          ]) {
            expect(
              rg[key],
              snapshot[key],
              reason: 'confirmation trace $i differs in $key',
            );
            expect(
              cas[key],
              snapshot[key],
              reason: 'CAS confirmation trace $i differs in $key',
            );
          }
        }
      }
    },
    timeout: const Timeout(Duration(minutes: 15)),
  );
}
