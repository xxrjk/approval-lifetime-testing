import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:approval_sample/audit_binding.dart';
import 'package:approval_sample/db.dart';
import 'package:approval_sample/models.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';
import 'lifecycle_test.dart' as fixture;
import 'admission_test.dart' as observation;

const variants = [
  'digest_control',
  'snapshot_control',
  'intake_only',
  'callback_regression',
  'epoch_rebinding',
];

Map<String, Object?> context(Map<String, Object?> row) => {
  'content': observation.binding(row),
  'authority': row['approval_epoch'],
};

Map<String, Object?> project(Map<String, Object?> row) {
  final metadata = jsonDecode(row['audit_metadata'] as String? ?? '{}') as Map;
  final quotes = jsonDecode(row['audit_quotes'] as String? ?? '[]') as List;
  final groups = jsonDecode(row['source_evidence_groups'] as String) as List;
  final probability = row['audit_error_probability'] as num?;
  final reviews = row['audit_expected_reviews'] as num;
  // Independent raw-field projection. Never call the implementation gate/hash
  // to label validity. Receipt binding is checked only by the shared Python core.
  final valid =
      row['audit_schema_version'] == CardAuditContract.schemaVersion &&
      (probability == null ||
          (probability.isFinite && probability >= 0 && probability <= 1)) &&
      reviews.isFinite &&
      reviews >= 0 &&
      (row['audit_model'] as String? ?? '').trim().isNotEmpty &&
      (row['audit_reason'] as String? ?? '').trim().isNotEmpty &&
      quotes.isNotEmpty &&
      quotes.every(
        (q) =>
            q is String &&
            q.trim().isNotEmpty &&
            groups.any((g) => g is String && g.contains(q)),
      );
  return {
    'context': context(row),
    'state': row['status'],
    'approval': metadata['packet_id'] == null
        ? null
        : {
            'origin': metadata['packet_id'],
            'positive':
                row['audit_status'] == AuditStatus.passed &&
                row['audit_answer_label'] == CardAuditContract.supported &&
                row['audit_decision'] == CardAuditContract.pass,
            'local_valid': valid,
          },
  };
}

Future<bool> complete(
  fixture.Engine engine,
  String variant,
  fixture.Request request,
  String id,
  bool positive,
) async {
  var used = request;
  final row = await engine.row();
  if (variant == 'epoch_rebinding') {
    final copy = request.card.copy();
    copy.approvalEpoch = row['approval_epoch'] as int;
    used = fixture.Request(copy, request.revision, request.ticket);
  }
  if (variant == 'callback_regression' &&
      request.card.approvalEpoch == row['approval_epoch'] &&
      cardAuditContentHash(request.card) !=
          cardAuditContentHash(CardItem.fromMap(row))) {
    // Historical callback write, reintroduced into the current epoch-enabled base.
    // Conditions and written fields match db_before_callback_fix.dart:659-682.
    await (await Db.db).update(
      'cards',
      {
        'status': CardStatus.pending,
        'audit_status': AuditStatus.stale,
        'audit_error_probability': null,
        'audit_priority': null,
        'audit_quotes': '[]',
        'audit_model': null,
        'audit_updated_at': null,
        'audit_reason': 'content changed while audit was in flight',
        'audit_spans': '[]',
        'audit_metadata': '{}',
        'audit_content_hash': null,
        'audit_schema_version': null,
        'audit_answer_label': null,
        'audit_decision': null,
      },
      where: 'id=? AND status=?',
      whereArgs: [engine.id, CardStatus.pending],
    );
    return false;
  }
  return engine.commit(
    used,
    quotes: positive ? [request.card.back] : [],
    label: positive ? CardAuditContract.supported : 'UNSUPPORTED',
    decision: positive ? CardAuditContract.pass : 'REJECT',
    auditStatus: positive ? AuditStatus.passed : AuditStatus.rejected,
    metadata: {'packet_id': id},
  );
}

void main() {
  setUpAll(() {
    sqfliteFfiInit();
    Db.factoryOverride = databaseFactoryFfi;
    Db.pathOverride = inMemoryDatabasePath;
  });
  tearDownAll(() async => Db.reset());
  test(
    'method-effectiveness: actual database observations',
    () async {
      const input = String.fromEnvironment('METHOD_INPUT');
      const output = String.fromEnvironment('METHOD_OUTPUT');
      final runs = jsonDecode(File(input).readAsStringSync()) as List;
      final out = File(output);
      out.parent.createSync(recursive: true);
      final fileSink = out.openWrite();
      final sink = gzip.encoder.startChunkedConversion(fileSink);
      void emit(Map<String, Object?> row) =>
          sink.add(utf8.encode('${jsonEncode(row)}\n'));
      var total = 0;
      for (final variant in variants) {
        for (final raw in runs) {
          final run = raw as Map;
          for (final rawHistory in run['histories'] as List) {
            final history = rawHistory as Map;
            final policy = variant == 'snapshot_control'
                ? 'snapshot_epoch'
                : variant == 'intake_only'
                ? 'ingest_only'
                : 'rehearsalguard';
            final engine = fixture.Engine(policy);
            await engine.seed();
            final requests = <String, fixture.Request>{};
            final ids = <String, String>{};
            var serial = 0;
            var step = 0;
            for (final event in history['events'] as List) {
              final before = project(await engine.row());
              final row = <String, Object?>{
                'variant': variant,
                'strategy': run['strategy'],
                'seed': run['seed'],
                'phase': run['phase'],
                'budget': run['budget'],
                'history': history['history'],
                'obligation': history['obligation'],
                'event': event,
                'operation': (history['offset'] as int) + 2 + step,
                'before': before,
                'kind': 'other',
              };
              if ((event as String).startsWith('request_')) {
                final slot = event.split('_')[1];
                final request = await engine.capture();
                requests[slot] = request;
                ids[slot] = '${serial++}';
                row['capture'] = {
                  'id': ids[slot],
                  'context': context(await engine.row()),
                };
              } else if (event.startsWith('complete_')) {
                final parts = event.split('_');
                final request = requests[parts[1]];
                // Primary generators never produce an uncaptured completion.
                // Deletion-based reduction may remove its capture: this is a no-op,
                // matching the original lifecycle harness's established semantics.
                if (request == null) {
                  row['uncaptured_completion'] = true;
                } else {
                  row['kind'] = 'complete';
                  row['applied'] = await complete(
                    engine,
                    variant,
                    request,
                    ids[parts[1]]!,
                    parts[2] == 'pos',
                  );
                  row['incoming_origin'] = ids[parts[1]];
                }
              } else if (event.startsWith('edit_')) {
                await engine.edit(event == 'edit_a' ? 'Alpha.' : 'Beta.');
              } else if (event == 'revoke') {
                await Db.revokeCardApproval(engine.id);
              } else if (event == 'suspend') {
                await Db.updateCardStatus(engine.id, CardStatus.suspended);
              } else if (event.startsWith('activate')) {
                final entry = event == 'activate_batch' ? 'batch' : 'single';
                row['kind'] = 'activate';
                row['entry'] = entry;
                row['released'] = await engine.activate(entry);
              } else {
                throw StateError('Unknown event $event');
              }
              row['after'] = project(await engine.row());
              emit(row);
              step++;
              total++;
            }
          }
          stdout.writeln(
            'METHOD ${run['phase']} $variant ${run['strategy']} ${run['seed']}',
          );
        }
      }
      sink.close();
      await fileSink.done;
      stdout.writeln(
        jsonEncode({
          'observed_steps': total,
          'variants': variants,
          'output': output,
        }),
      );
    },
    timeout: const Timeout(Duration(minutes: 30)),
  );
}
