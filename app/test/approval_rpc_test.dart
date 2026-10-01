// Local experiment bridge. All generators invoke the same actual database core.
import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:approval_sample/db.dart';
import 'package:approval_sample/models.dart';
import 'package:sqflite_common_ffi/sqflite_ffi.dart';
import 'lifecycle_test.dart' as fixture;
import 'effects_test.dart' as methods;

class Runtime {
  late fixture.Engine engine;
  String variant = 'digest_control';
  String field = 'answer';
  final requests = <String, fixture.Request>{};
  final identifiers = <String, String>{};
  int serial = 0;
  int originalBook = 0;
  int alternateBook = 0;
  int? alternateChapter;
  late Map<String, Object?> originalRow;
  final objectIds = <int>[];
  bool expanded = false;

  Future<bool> finish(
    fixture.Request request,
    String identifier,
    bool positive,
  ) async {
    final row = await engine.row();
    final staleContent =
        fixture.Engine.snapshot(request.card) !=
        fixture.Engine.snapshot(CardItem.fromMap(row));
    if (variant == 'callback_lies_applied') {
      final applied = await methods.complete(
        engine,
        'callback_regression',
        request,
        identifier,
        positive,
      );
      return staleContent && request.card.approvalEpoch == row['approval_epoch']
          ? true
          : applied;
    }
    if (staleContent && row['status'] == CardStatus.pending) {
      if (variant == 'refusal_origin_clear') {
        await engine.inject({'audit_metadata': '{}'});
        return false;
      }
      if (variant == 'refusal_state_write') {
        await engine.inject({'status': CardStatus.suspended});
        return false;
      }
    }
    if (variant == 'active_refusal_downgrade' &&
        row['status'] == CardStatus.active) {
      await engine.inject({'status': CardStatus.pending});
      return false;
    }
    if (variant == 'suspended_refusal_clear' &&
        row['status'] == CardStatus.suspended) {
      await engine.inject({'audit_status': AuditStatus.stale});
      return false;
    }
    var used = request;
    if (variant == 'content_rebinding') {
      final current = CardItem.fromMap(row);
      current.approvalEpoch = request.card.approvalEpoch;
      used = fixture.Request(current, request.revision, request.ticket);
    } else if (const [
      'evidence_omitted',
      'source_omitted',
      'question_omitted',
    ].contains(variant)) {
      final copy = request.card.toMap();
      final keys = variant == 'source_omitted'
          ? ['book_id']
          : variant == 'question_omitted'
          ? ['front']
          : [
              'source_evidence',
              'source_context',
              'source_evidence_groups',
              'source_evidence_start',
              'source_evidence_end',
            ];
      for (final key in keys) {
        copy[key] = row[key];
      }
      used = fixture.Request(
        CardItem.fromMap(copy),
        request.revision,
        request.ticket,
      );
    }
    final suspendedBypass =
        variant == 'suspended_negative_write' &&
        row['status'] == CardStatus.suspended;
    if (suspendedBypass) await engine.inject({'status': CardStatus.pending});
    final applied = identical(used, request)
        ? await methods.complete(engine, variant, request, identifier, positive)
        : await engine.commit(
            used,
            quotes: positive ? [request.card.back] : [],
            label: positive ? CardAuditContract.supported : 'UNSUPPORTED',
            decision: positive ? CardAuditContract.pass : 'REJECT',
            auditStatus: positive ? AuditStatus.passed : AuditStatus.rejected,
            metadata: {'packet_id': identifier},
          );
    if (suspendedBypass) await engine.inject({'status': CardStatus.suspended});
    return applied;
  }

  Future<void> edit(String value) async {
    final current = await engine.row();
    if (field == 'answer') {
      await engine.edit(value == 'a' ? 'Alpha.' : 'Beta.');
      return;
    }
    if (field == 'question') {
      await Db.updateCardText(
        engine.id,
        value == 'a' ? 'Which token?' : 'Which exact token?',
        current['back'] as String,
      );
      return;
    }
    // Synthetic source replacement shares explicit invalidation across all designs.
    await Db.updateCardText(
      engine.id,
      current['front'] as String,
      current['back'] as String,
    );
    if (field == 'source') {
      engine.book = value == 'a' ? originalBook : alternateBook;
      await engine.inject({'book_id': engine.book});
    } else if (field == 'evidence') {
      await engine.inject(
        value == 'a'
            ? {
                for (final key in [
                  'source_evidence',
                  'source_context',
                  'source_evidence_groups',
                  'source_evidence_start',
                  'source_evidence_end',
                ])
                  key: originalRow[key],
              }
            : {
                'source_evidence': 'Alpha.',
                'source_context': 'Changed evidence contains Alpha. and Beta.',
                'source_evidence_groups': jsonEncode([
                  'Changed evidence contains Alpha. and Beta.',
                ]),
              },
      );
    } else if (const [
      'chapter',
      'type',
      'page',
      'context',
      'blocks',
      'offset_start',
      'offset_end',
      'quote',
    ].contains(field)) {
      final key = const {
        'chapter': 'chapter_id',
        'type': 'type',
        'page': 'source_page',
        'context': 'source_context',
        'blocks': 'source_evidence_groups',
        'offset_start': 'source_evidence_start',
        'offset_end': 'source_evidence_end',
        'quote': 'source_evidence',
      }[field]!;
      final replacement = switch (field) {
        'chapter' =>
          alternateChapter ??= await (await Db.db).insert('chapters', {
            'book_id': engine.book,
            'idx': 1,
            'title': 'Another chapter',
            'page_start': 1,
            'page_end': 2,
            'char_count': 1,
          }),
        'type' => 'cloze',
        'page' => 2,
        'context' => 'Another window containing Alpha. and Beta.',
        'blocks' => jsonEncode(['Different block contains Alpha. and Beta.']),
        'offset_start' => 2,
        'offset_end' => 9,
        'quote' => 'Beta.',
        _ => throw StateError(field),
      };
      await engine.inject({key: value == 'a' ? originalRow[key] : replacement});
    } else {
      throw StateError('Unknown declared edit field $field');
    }
  }

  Map<String, Object?> project(Map<String, Object?> row) {
    final result = methods.project(row);
    result['context'] = {
      ...result['context'] as Map<String, Object?>,
      'object': row['id'],
    };
    return result;
  }

  Future<Map<String, Object?>> observe(Map<String, Object?> row) async {
    final result = project(row);
    if (expanded && engine.tracked) {
      final d = await Db.db;
      final revisions = await d.query(
        'revisions',
        where: 'card_id=?',
        whereArgs: [row['id']],
      );
      final context = result['context'] as Map<String, Object?>;
      context['revision'] = revisions.single['obj'];
      if (engine.policy == 'latest_request_tx') {
        final tickets = await d.query(
          'requests',
          where: 'card_id=?',
          whereArgs: [row['id']],
        );
        context['latest'] = tickets.single['ticket'];
      }
    }
    return result;
  }

  Future<Map<String, Object?>> command(Map<String, dynamic> command) async {
    if (command['op'] == 'inspect_all') {
      final rows = await (await Db.db).query('cards', orderBy: 'id');
      return {'objects': rows.map(project).toList()};
    }
    if (command['op'] == 'select') {
      engine.id = command['id'] as int;
      engine.book = (await engine.row())['book_id'] as int;
      return {'observation': project(await engine.row())};
    }
    if (command['op'] == 'neighbor') {
      if (variant != 'digest_control' && !expanded)
        throw StateError('Neighbor probe uses the real application design');
      final same = command['identical'] == true;
      await Db.insertDraftCards(engine.book, null, [
        DraftCard(
          type: 'qa',
          front: 'Which token?',
          back: same ? 'Alpha.' : 'Beta.',
          page: 1,
          evidenceQuote: same ? 'Alpha.' : 'Beta.',
          sourceContext: 'One block contains Alpha.',
          evidenceGroups: const [
            'One block contains Alpha.',
            'Second block contains Beta.',
          ],
        ),
      ]);
      final candidates = await Db.pendingCards(engine.book);
      final id = candidates
          .map((c) => c.id)
          .where((id) => id != engine.id)
          .last;
      if (engine.tracked) {
        await (await Db.db).insert('revisions', {
          'card_id': id,
          'obj': 0,
          'audit': 0,
        });
        if (engine.policy == 'latest_request_tx') {
          await (await Db.db).insert('requests', {'card_id': id, 'ticket': 0});
        }
      }
      objectIds.add(id);
      return {'id': id};
    }
    if (command['op'] == 'export_session') {
      return {
        'session': {
          'variant': variant,
          'field': field,
          'id': engine.id,
          'book': engine.book,
          'original_book': originalBook,
          'alternate_book': alternateBook,
          'original_row': originalRow,
          'serial': serial,
          'identifiers': identifiers,
          'requests': {
            for (final entry in requests.entries)
              entry.key: {
                'card': entry.value.card.toMap(),
                'revision': entry.value.revision,
                'ticket': entry.value.ticket,
              },
          },
        },
      };
    }
    if (command['op'] == 'attach') {
      await Db.reset();
      Db.pathOverride = command['path'] as String;
      final state = command['session'] as Map<String, dynamic>;
      variant = state['variant'] as String;
      field = state['field'] as String;
      engine = fixture.Engine('rehearsalguard');
      engine.id = state['id'] as int;
      engine.book = state['book'] as int;
      originalBook = state['original_book'] as int;
      alternateBook = state['alternate_book'] as int;
      originalRow = Map<String, Object?>.from(state['original_row'] as Map);
      serial = state['serial'] as int;
      identifiers.clear();
      identifiers.addAll(Map<String, String>.from(state['identifiers'] as Map));
      requests.clear();
      for (final entry in (state['requests'] as Map<String, dynamic>).entries) {
        final data = entry.value as Map<String, dynamic>;
        requests[entry.key] = fixture.Request(
          CardItem.fromMap(Map<String, Object?>.from(data['card'] as Map)),
          data['revision'] as int,
          data['ticket'] as int,
        );
      }
      return {'observation': project(await engine.row())};
    }
    if (command['op'] == 'reset') {
      alternateChapter = null;
      if (command['path'] != null || Db.pathOverride != inMemoryDatabasePath) {
        await Db.reset();
        Db.pathOverride = command['path'] as String? ?? inMemoryDatabasePath;
      }
      if (command['path'] != null && File(Db.pathOverride!).existsSync()) {
        await databaseFactoryFfi.deleteDatabase(Db.pathOverride!);
      }
      variant = command['variant'] as String;
      field = command['field'] as String? ?? 'answer';
      expanded = command['expanded'] == true;
      engine = fixture.Engine(
        command['design'] as String? ??
            (variant == 'snapshot_control'
                ? 'snapshot_epoch'
                : variant == 'intake_only'
                ? 'ingest_only'
                : 'rehearsalguard'),
      );
      final watch = Stopwatch()..start();
      await engine.seed();
      originalBook = engine.book;
      originalRow = await engine.row();
      alternateBook = await Db.insertBook(
        'alternate synthetic source',
        'alternate.pdf',
        2,
        false,
      );
      requests.clear();
      identifiers.clear();
      serial = 0;
      objectIds.clear();
      objectIds.add(engine.id);
      return {
        'reset': true,
        'target_us': watch.elapsedMicroseconds,
        'observation': await observe(await engine.row()),
      };
    }
    final event = command['event'] as String;
    final observationWatch = Stopwatch()..start();
    final before = await observe(await engine.row());
    observationWatch.stop();
    final record = <String, Object?>{
      'kind': 'other',
      'event': event,
      'before': before,
    };
    final targetWatch = Stopwatch()..start();
    if (event.startsWith('request_')) {
      final slot = event.split('_')[1];
      requests[slot] = await engine.capture();
      identifiers[slot] = '${serial++}';
      // Value snapshot is serialized before any fault adapter can mutate a copy.
      record['capture'] = {
        'id': identifiers[slot],
        'context': jsonDecode(
          jsonEncode((await observe(await engine.row()))['context']),
        ),
      };
    } else if (event.startsWith('complete_')) {
      final parts = event.split('_');
      final request = requests[parts[1]];
      if (request == null) {
        record['uncaptured_completion'] = true;
      } else {
        record['kind'] = 'complete';
        record['incoming_origin'] = identifiers[parts[1]];
        final validInput = !parts[2].startsWith('bad');
        final positive = parts[2].endsWith('pos');
        record['incoming_valid'] = validInput;
        record['incoming_positive'] = positive;
        if (validInput) {
          record['applied'] = await finish(
            request,
            identifiers[parts[1]]!,
            positive,
          );
        } else {
          // Supply an independently invalid numeric input to the typed API.
          // Validation rejects it before the transaction begins.
          // Record the resulting exception as the completion outcome.
          try {
            record['applied'] = await Db.updateCardAudit(
              id: engine.id,
              auditStatus: positive ? AuditStatus.passed : AuditStatus.rejected,
              errorProbability: 0,
              expectedReviews: -1,
              evidenceQuotes: ['Alpha.'],
              model: 'numeric-invalid-input',
              reason: 'programmed invalid input',
              schemaVersion: CardAuditContract.schemaVersion,
              answerLabel: positive
                  ? CardAuditContract.supported
                  : 'UNSUPPORTED',
              decision: positive ? CardAuditContract.pass : 'REJECT',
              metadata: {'packet_id': identifiers[parts[1]]},
            );
          } on ArgumentError {
            record['applied'] = false;
            record['input_rejection'] = 'ArgumentError';
          }
        }
      }
    } else if (event.startsWith('edit_')) {
      await edit(event == 'edit_a' ? 'a' : 'b');
    } else if (event == 'revoke') {
      await Db.revokeCardApproval(engine.id);
    } else if (event == 'suspend') {
      await Db.updateCardStatus(engine.id, CardStatus.suspended);
    } else if (event.startsWith('activate')) {
      final entry = event == 'activate_batch' ? 'batch' : 'single';
      record['kind'] = 'activate';
      record['entry'] = entry;
      record['released'] = await engine.activate(entry);
    } else if (event.startsWith('select_')) {
      engine.id = objectIds[int.parse(event.split('_')[1])];
      engine.book = (await engine.row())['book_id'] as int;
    } else {
      throw StateError('Unknown event $event');
    }
    targetWatch.stop();
    observationWatch.start();
    record['after'] = await observe(await engine.row());
    observationWatch.stop();
    return {
      'record': record,
      'target_us': targetWatch.elapsedMicroseconds,
      'observation_us': observationWatch.elapsedMicroseconds,
    };
  }
}

void main() {
  test('local approval experiment bridge', () async {
    sqfliteFfiInit();
    Db.factoryOverride = databaseFactoryFfi;
    Db.pathOverride = inMemoryDatabasePath;
    const ready = String.fromEnvironment('RPC_READY');
    final server = await ServerSocket.bind(InternetAddress.loopbackIPv4, 0);
    File(ready).writeAsStringSync(jsonEncode({'port': server.port}));
    final done = Completer<void>();
    final runtime = Runtime();
    server.listen((socket) async {
      socket.setOption(SocketOption.tcpNoDelay, true);
      await for (final line
          in socket
              .cast<List<int>>()
              .transform(utf8.decoder)
              .transform(const LineSplitter())) {
        final request = jsonDecode(line) as Map<String, dynamic>;
        if (request['op'] == 'shutdown') {
          socket.writeln(jsonEncode({'ok': true}));
          await socket.flush();
          await socket.close();
          if (!done.isCompleted) done.complete();
          break;
        }
        try {
          socket.writeln(
            jsonEncode({'ok': true, ...await runtime.command(request)}),
          );
        } catch (error, stack) {
          socket.writeln(
            jsonEncode({'ok': false, 'error': '$error', 'stack': '$stack'}),
          );
        }
        await socket.flush();
      }
    });
    await done.future;
    await server.close();
    await Db.reset();
  }, timeout: const Timeout(Duration(hours: 12)));
}
