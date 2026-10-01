// Deterministic binding between an audit result and the exact persisted card.
//
// Hashing uses the stored code-unit strings without Unicode normalization. This
// is deliberate: normalization, OCR repair, or re-extraction is a content
// change that requires a new audit before approval can be used.
library;

import 'dart:convert';

import 'package:crypto/crypto.dart';

import 'models.dart';

const auditContentBindingSchema = 'rehearsalguard.audit.content-binding.v2';

String cardAuditContentHash(CardItem card) {
  final payload = jsonEncode({
    'schema_version': auditContentBindingSchema,
    'book_id': card.bookId,
    'chapter_id': card.chapterId,
    'type': card.type,
    'front': card.front,
    'back': card.back,
    'source_page': card.sourcePage,
    'source_evidence': card.sourceEvidence,
    'source_context': card.sourceContext,
    'source_evidence_groups': card.sourceEvidenceGroups,
    'source_evidence_start': card.sourceEvidenceStart,
    'source_evidence_end': card.sourceEvidenceEnd,
  });
  return sha256.convert(utf8.encode(payload)).toString();
}
