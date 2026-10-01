// 统计页用的纯计算:打卡日计数、连续打卡、未来到期分桶。
// 都是纯函数不碰数据库,好测。时区转换集中在这,别散到页面里
library;

import 'dart:math';

import 'db.dart';
import 'fsrs.dart';
import 'models.dart';

// review_logs 的一条轻量行。reviewed_at 存的是 UTC,这里进来就转成本地时间,
// 后面所有按日聚合都用本地日,跨时区/跨0点不会算错
class ReviewLogRow {
  final int cardId;
  final int rating;
  final DateTime at; // 已转本地时间
  final int durationMs;

  ReviewLogRow(this.cardId, this.rating, this.at, this.durationMs);

  ReviewLogRow.fromMap(Map<String, Object?> m)
    : cardId = m['card_id'] as int,
      rating = m['rating'] as int,
      at = DateTime.parse(m['reviewed_at'] as String).toLocal(),
      durationMs = (m['duration_ms'] as int?) ?? 0;
}

// 每个本地日背过的卡片数。口径是 DISTINCT card_id:同一张卡当天反复
// 忘记/重学点了多少次都只算 1 张,打卡日历和"今日已背"都用这个
Map<String, int> localDayCardCounts(List<ReviewLogRow> rows) {
  final byDay = <String, Set<int>>{};
  for (final r in rows) {
    (byDay[Db.localDay(r.at)] ??= {}).add(r.cardId);
  }
  return {for (final e in byDay.entries) e.key: e.value.length};
}

// 每个本地日的"认知分布":同一张卡同一天点过多次,取当天最后一次评分做最终分类
// (先忘记后认识,最终算认识)。返回 day -> (rating -> 卡片数)。
// 要求 rows 按时间先后有序,Db.reviewLogRows 按 id 查出来就是
Map<String, Map<int, int>> dailyFinalRatings(List<ReviewLogRow> rows) {
  final lastByDay = <String, Map<int, int>>{}; // day -> cardId -> 最后一次评分
  for (final r in rows) {
    (lastByDay[Db.localDay(r.at)] ??= {})[r.cardId] = r.rating;
  }
  final out = <String, Map<int, int>>{};
  for (final e in lastByDay.entries) {
    final counts = <int, int>{};
    for (final g in e.value.values) {
      counts[g] = (counts[g] ?? 0) + 1;
    }
    out[e.key] = counts;
  }
  return out;
}

// 每个本地日的答题总用时(毫秒)
Map<String, int> dailyDurationMs(List<ReviewLogRow> rows) {
  final out = <String, int>{};
  for (final r in rows) {
    final day = Db.localDay(r.at);
    out[day] = (out[day] ?? 0) + r.durationMs;
  }
  return out;
}

// 连续打卡天数:从今天往回数,背过算打卡。今天还没背的话从昨天起算(今天补上就+1)
int currentStreak(Map<String, int> dayCounts, DateTime now) {
  var day = DateTime(now.year, now.month, now.day);
  if (!dayCounts.containsKey(Db.localDay(day))) {
    day = day.subtract(const Duration(days: 1));
  }
  var streak = 0;
  while (dayCounts.containsKey(Db.localDay(day))) {
    streak += 1;
    day = day.subtract(const Duration(days: 1));
  }
  return streak;
}

// 复习卡的到期时间(UTC ISO)按本地日分桶,返回长度 days 的数组:
// [0]=今天(含所有逾期的),[1]=明天,……超出范围的丢掉
List<int> bucketDueByDay(List<String> dueIsoList, DateTime now, int days) {
  final today = DateTime(now.year, now.month, now.day);
  final out = List<int>.filled(days, 0);
  for (final iso in dueIsoList) {
    final due = DateTime.parse(iso).toLocal();
    final dueDay = DateTime(due.year, due.month, due.day);
    var idx = dueDay.difference(today).inDays;
    if (idx < 0) idx = 0; // 逾期的都算今天要还的债
    if (idx < days) out[idx] += 1;
  }
  return out;
}

// 未来每天要背多少张:不是只看已排程的到期时间,而是拿 FSRS 真跑一遍模拟——
// 每张卡从当前状态一路推到窗口外,路过哪天哪天+1
// (同一张卡一天只计一次;学习步1分钟/10分钟在同一天内不重复计)。
// 关键:不假设每次都记得。到期时按遗忘曲线算出这张卡当时的记得概率 R,
// 掷骰子决定走「记得」还是「忘记」——忘了就进重学,当天多背、更快回来,
// 所以"平均会忘掉一成"已经算在预测里了。骰子种子取卡id,同样的数据推出来
// 永远一样,图不会一刷新就变。困难/轻松不单独模拟:它们不改变要背的张数,
// 只是间隔的微调,对每天数量的影响远小于忘没忘。
// 没背过的新卡按每日上限逐天入队:今天用剩余额度,之后每天 newPerDay 张。
List<int> simulateWorkload(
  List<CardItem> cards, {
  required DateTime now,
  required int days,
  required int newPerDay,
  required int introducedToday,
}) {
  final fsrs = Fsrs(seed: 1);
  final today = DateTime(now.year, now.month, now.day);
  final out = List<int>.filled(days, 0);

  void walk(CardItem start, DateTime firstAtUtc) {
    var card = start.copy();
    var at = firstAtUtc;
    final rng = Random(start.id * 40503 + 7);
    final seenDays = <int>{};
    // 复习间隔毕业后至少1天,窗口才14天,64次上限纯粹是保险丝。
    // 同一天内的重学/学习步 elapsed<1天,R按1算(刚背过还热乎),不会死循环
    for (var i = 0; i < 64; i++) {
      final local = at.toLocal();
      final day = DateTime(local.year, local.month, local.day);
      var idx = day.difference(today).inDays;
      if (idx < 0) idx = 0;
      if (idx >= days) break;
      if (seenDays.add(idx)) out[idx] += 1;
      var r = 1.0;
      if (card.state == CardState.review &&
          card.stability != null &&
          card.lastReview != null) {
        final elapsed =
            at.difference(card.lastReview!).inMilliseconds / 86400000.0;
        if (elapsed >= 1) r = fsrs.retrievability(elapsed, card.stability!);
      }
      final rating = rng.nextDouble() < r ? Rating.good : Rating.again;
      card = fsrs.review(card, rating, at, fuzz: false).card;
      at = card.due;
    }
  }

  final nowUtc = now.toUtc();
  final news = <CardItem>[];
  for (final c in cards) {
    if (c.state == CardState.newCard) {
      news.add(c);
    } else {
      // 逾期的当作今天马上背
      walk(c, c.due.isBefore(nowUtc) ? nowUtc : c.due);
    }
  }
  if (newPerDay > 0) {
    var i = 0;
    for (var d = 0; d < days && i < news.length; d++) {
      var quota = d == 0
          ? (newPerDay - introducedToday).clamp(0, newPerDay)
          : newPerDay;
      final introAt = nowUtc.add(Duration(days: d));
      while (quota > 0 && i < news.length) {
        walk(news[i], introAt);
        i += 1;
        quota -= 1;
      }
    }
  }
  return out;
}

// ---------- 遗忘曲线 tab ----------

// 你的学习遗忘曲线:假设今天刚复习完、之后不再碰,未来第 t 天全体已学卡片的
// 平均记忆保持率——FSRS 的 R(t,S) 按每张卡自己的稳定度算,再取平均。
// 返回百分数列表,下标 0=今天(恒为100)。没有已学卡片返回空列表,页面画占位提示。
// 复习数据越多、稳定度越贴近本人,这条线就越准,所以文案写"用得越久预测越贴近"
List<double> forgettingCurve(List<CardItem> cards, int days) {
  final fsrs = Fsrs();
  final stabilities = [
    for (final c in cards)
      if (c.state != CardState.newCard && c.stability != null) c.stability!,
  ];
  if (stabilities.isEmpty) return const [];
  return [
    for (var t = 0; t < days; t++)
      100 *
          stabilities
              .map((s) => fsrs.retrievability(t.toDouble(), s))
              .reduce((a, b) => a + b) /
          stabilities.length,
  ];
}

// 艾宾浩斯经典遗忘曲线参考线:1885 年实验的记忆保持率数据点,线性插值。
// 只是给个公认参照,不是用户自己的数据,更不等同任何商业软件的内部算法
List<double> ebbinghausCurve(int days) {
  const pts = <(double, double)>[
    (0, 100),
    (1, 33.7),
    (2, 27.8),
    (6, 25.4),
    (31, 21.1),
  ];
  double at(double t) {
    for (var i = 1; i < pts.length; i++) {
      if (t <= pts[i].$1) {
        final (x0, y0) = pts[i - 1];
        final (x1, y1) = pts[i];
        return y0 + (y1 - y0) * (t - x0) / (x1 - x0);
      }
    }
    return pts.last.$2;
  }

  return [for (var t = 0; t < days; t++) at(t.toDouble())];
}

// ---------- 记忆持久度 tab ----------

// 记忆持久度分桶:稳定度 ≥ 阈值天数的卡片各有多少张。
// 「已加入规划」的口径:传进来的卡(active)里已开始学习的(state != newCard)——
// 新卡还没有稳定度,塞进分母只会把比例稀释得没意义;选这个口径并在页面注明
({int planned, Map<int, int> buckets}) stabilityBuckets(
  List<CardItem> cards,
  List<int> thresholds,
) {
  var planned = 0;
  final buckets = {for (final t in thresholds) t: 0};
  for (final c in cards) {
    if (c.state == CardState.newCard || c.stability == null) continue;
    planned += 1;
    for (final t in thresholds) {
      if (c.stability! >= t) buckets[t] = buckets[t]! + 1;
    }
  }
  return (planned: planned, buckets: buckets);
}

({int planned, Map<int, int> buckets}) quizStabilityBuckets(
  List<QuizQuestion> questions,
  List<int> thresholds,
) {
  var planned = 0;
  final buckets = {for (final t in thresholds) t: 0};
  for (final q in questions) {
    if (q.qState == CardState.newCard || q.qStability == null) continue;
    planned += 1;
    for (final t in thresholds) {
      if (q.qStability! >= t) buckets[t] = buckets[t]! + 1;
    }
  }
  return (planned: planned, buckets: buckets);
}
