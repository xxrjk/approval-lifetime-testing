// FSRS-5 间隔重复算法,就是新版 Anki 在用的那个调度算法。
// 公式和那19个默认参数来自开源项目 open-spaced-repetition/fsrs(FSRS-5版),
// Dart 实现依据论文公式和 Python 参考实现。
// 核心就一条遗忘曲线: R(t,S) = (1 + FACTOR*t/S)^DECAY
// DECAY=-0.5, FACTOR=19/81, 这么凑是为了 t=S 的时候 R 正好等于90%
library;

import 'dart:math';

import 'models.dart';

// 四档评分,数值跟 Anki/FSRS 对齐(按钮文案是墨墨式的:忘记/模糊/认识/熟知)。
// 注意语义:hard(模糊)是"通过了但吃力",不算失败;只有 again(忘记)才是真忘记
class Rating {
  static const again = 1;
  static const hard = 2;
  static const good = 3;
  static const easy = 4;
}

// FSRS-5 参数,一共19个。w[3] 按本 App 的首轮熟知体验降到 5 天,
// 避免新卡第一次熟知直接跳到半个月后;后续熟知仍由 FSRS easyBonus 拉长。
const List<double> _w = [
  0.40255,
  1.18385,
  3.173,
  5.0,
  7.1949,
  0.5345,
  1.4604,
  0.0046,
  1.54575,
  0.1192,
  1.01925,
  1.9395,
  0.11,
  0.29605,
  2.2698,
  0.2315,
  2.9898,
  0.51655,
  0.6621,
];
const double _decay = -0.5;
const double _factor = 19 / 81;

class ReviewResult {
  final CardItem card;
  final Duration interval;
  ReviewResult(this.card, this.interval);
}

class Fsrs {
  final double desiredRetention;
  final List<Duration> learningSteps;
  final List<Duration> relearningSteps;
  final int maximumIntervalDays;
  final Random _rng;

  Fsrs({
    this.desiredRetention = 0.9,
    this.learningSteps = const [Duration(minutes: 1), Duration(minutes: 10)],
    this.relearningSteps = const [Duration(minutes: 10)],
    this.maximumIntervalDays = 3650,
    int? seed,
  }) : _rng = Random(seed);

  double retrievability(double elapsedDays, double stability) =>
      pow(1 + _factor * elapsedDays / stability, _decay).toDouble();

  double _initStability(int g) => max(_w[g - 1], 0.1);

  double _clampD(double d) => min(10.0, max(1.0, d));

  double _initDifficulty(int g) => _clampD(_w[4] - exp(_w[5] * (g - 1)) + 1);

  double _nextDifficulty(double d, int g) {
    final delta = -_w[6] * (g - 3);
    final damped = d + delta * (10 - d) / 9;
    final target = _w[4] - exp(_w[5] * 3) + 1; // D0(easy),不 clamp,作回归目标
    return _clampD(_w[7] * target + (1 - _w[7]) * damped);
  }

  double _recallStability(double d, double s, double r, int g) {
    final hardPenalty = g == Rating.hard ? _w[15] : 1.0;
    final easyBonus = g == Rating.easy ? _w[16] : 1.0;
    return s *
        (1 +
            exp(_w[8]) *
                (11 - d) *
                pow(s, -_w[9]) *
                (exp(_w[10] * (1 - r)) - 1) *
                hardPenalty *
                easyBonus);
  }

  double _forgetStability(double d, double s, double r) {
    final sf =
        _w[11] *
        pow(d, -_w[12]) *
        (pow(s + 1, _w[13]) - 1) *
        exp(_w[14] * (1 - r));
    return min(sf.toDouble(), s);
  }

  // 同一天内反复背同一张卡走这个,FSRS-5新加的短期记忆公式
  double _shortTermStability(double s, int g) =>
      s * exp(_w[17] * (g - 3 + _w[18]));

  // 稳定度 -> 间隔天数
  int intervalDays(double stability) {
    final ivl = stability / _factor * (pow(desiredRetention, 1 / _decay) - 1);
    return ivl.round().clamp(1, maximumIntervalDays);
  }

  // 打一次分,返回更新后的卡片副本和下次间隔。
  // 注意这个函数是纯计算不碰数据库,落库在 Db.applyReview 里做
  ReviewResult review(
    CardItem original,
    int rating,
    DateTime now, {
    bool fuzz = true,
  }) {
    assert(rating >= 1 && rating <= 4);
    final card = original.copy();

    // 先更新记忆参数(稳定度/难度)
    if (card.stability == null || card.difficulty == null) {
      card.stability = _initStability(rating);
      card.difficulty = _initDifficulty(rating);
    } else {
      final elapsedDays = card.lastReview == null
          ? 0.0
          : now.difference(card.lastReview!).inMilliseconds / 86400000.0;
      card.difficulty = _nextDifficulty(card.difficulty!, rating);
      if (elapsedDays < 1.0) {
        // 短期记忆公式只对已毕业的卡生效。学习步内固定走 1m/10m,
        // 否则第二次点「认识」会把首轮复习间隔放大到十几天。
        if (original.state >= CardState.review) {
          card.stability = _shortTermStability(card.stability!, rating);
        }
      } else {
        final r = retrievability(elapsedDays, card.stability!);
        card.stability = rating == Rating.again
            ? _forgetStability(card.difficulty!, card.stability!, r)
            : _recallStability(card.difficulty!, card.stability!, r, rating);
      }
    }

    // 然后是学习阶段的状态机,顺便把下次到期时间算出来
    Duration next;
    switch (card.state) {
      case CardState.newCard:
      case CardState.learning:
        if (card.state == CardState.newCard) {
          card.state = CardState.learning;
          card.step = 0;
        }
        if (rating == Rating.again) {
          card.step = 0;
          next = learningSteps.first;
        } else if (rating == Rating.hard) {
          // hard就停在当前步不动。在第一步时取前两步的中间值,anki的老规矩
          if (card.step == 0 && learningSteps.length > 1) {
            next = Duration(
              milliseconds:
                  (learningSteps[0].inMilliseconds +
                      learningSteps[1].inMilliseconds) ~/
                  2,
            );
          } else {
            next = learningSteps[card.step.clamp(0, learningSteps.length - 1)];
          }
        } else if (rating == Rating.good) {
          if (card.step + 1 < learningSteps.length) {
            card.step += 1;
            next = learningSteps[card.step];
          } else {
            card.state = CardState.review;
            card.step = 0;
            next = Duration(days: intervalDays(card.stability!));
          }
        } else {
          card.state = CardState.review;
          card.step = 0;
          next = Duration(days: intervalDays(card.stability!));
        }
      case CardState.relearning:
        if (rating == Rating.again) {
          card.step = 0;
          next = relearningSteps.first;
        } else if (rating == Rating.hard) {
          next =
              relearningSteps[card.step.clamp(0, relearningSteps.length - 1)];
        } else {
          card.state = CardState.review;
          card.step = 0;
          next = Duration(days: intervalDays(card.stability!));
        }
      default: // CardState.review
        if (rating == Rating.again) {
          card.lapses += 1;
          card.state = CardState.relearning;
          card.step = 0;
          next = relearningSteps.first;
        } else {
          var days = intervalDays(card.stability!);
          if (fuzz && days >= 3) {
            final f = (days * 0.05).clamp(1.0, 4.0).round();
            days = (days + _rng.nextInt(2 * f + 1) - f).clamp(
              1,
              maximumIntervalDays,
            );
          }
          next = Duration(days: days);
        }
    }

    card.reps += 1;
    card.lastReview = now;
    card.due = now.add(next);
    return ReviewResult(card, next);
  }

  // 四个评分按钮上的间隔预览。fuzz必须关掉,不然预览的数字每次都在跳
  Map<int, Duration> preview(CardItem card, DateTime now) => {
    for (final g in const [1, 2, 3, 4])
      g: review(card, g, now, fuzz: false).interval,
  };

  // 评分按钮上的间隔预览文案,墨墨式表达:1分钟/10分钟这种学习步不直接亮给用户,
  // 统一显示「今日」;会当天回流的评分,再模拟回流后一路点「认识」直到毕业,
  // 拿到进入复习后的天级间隔,显示成「今日 / X天后」。纯显示口径,内部 due 一分不改
  Map<int, String> previewLabels(CardItem card, DateTime now) => {
    for (final g in const [1, 2, 3, 4]) g: _previewLabel(card, g, now),
  };

  // 研究版风险代理:假设之后每次都点"认识",计算未来窗口内还会展示几次。
  // 返回固定评分路径下的确定性复习次数投影。
  int projectedGoodPathReviews(
    CardItem original,
    DateTime now, {
    int horizonDays = 30,
    int maxReviews = 100,
  }) {
    if (horizonDays < 0) {
      throw ArgumentError.value(
        horizonDays,
        'horizonDays',
        'must be non-negative',
      );
    }
    if (maxReviews <= 0) {
      throw ArgumentError.value(maxReviews, 'maxReviews', 'must be positive');
    }
    final horizon = now.add(Duration(days: horizonDays));
    var card = original.copy();
    var at = card.state == CardState.newCard || !card.due.isAfter(now)
        ? now
        : card.due;
    var count = 0;
    while (!at.isAfter(horizon) && count < maxReviews) {
      count += 1;
      final result = review(card, Rating.good, at, fuzz: false);
      card = result.card;
      at = card.due;
    }
    return count;
  }

  String _previewLabel(CardItem card, int rating, DateTime now) {
    var r = review(card, rating, now, fuzz: false);
    if (r.interval.inDays >= 1) return _daysLabel(r.interval);
    // 当天回流:学习步最多两级,8次上限只是保险丝
    var c = r.card;
    var at = c.due;
    for (var i = 0; i < 8; i++) {
      r = review(c, Rating.good, at, fuzz: false);
      if (r.interval.inDays >= 1) return '今日 / ${_daysLabel(r.interval)}';
      c = r.card;
      at = c.due;
    }
    return '今日';
  }

  static String _daysLabel(Duration d) => '${d.inDays}天后';
}

// 卡片熟练度 0~1,给进度条用。口径:稳定度到21天算满(Anki"成熟卡"的老标准),
// 也就是背到"三周后还记得90%"这张卡就算熟练了。没背过的是0
double masteryOf(CardItem card) {
  if (card.state == CardState.newCard || card.stability == null) return 0;
  return (card.stability! / 21.0).clamp(0.0, 1.0);
}

// Duration 转成"10分钟""3天""2.5个月"这种给人看的短文字
String humanizeDuration(Duration d) {
  if (d.inMinutes < 1) return '<1分钟';
  if (d.inMinutes < 60) return '${d.inMinutes}分钟';
  if (d.inHours < 36) return '${d.inHours}小时';
  if (d.inDays < 31) return '${d.inDays}天';
  String trim(String s) => s.endsWith('.0') ? s.substring(0, s.length - 2) : s;
  if (d.inDays < 365) {
    return '${trim((d.inDays / 30.4).toStringAsFixed(1))}个月';
  }
  return '${trim((d.inDays / 365).toStringAsFixed(1))}年';
}
