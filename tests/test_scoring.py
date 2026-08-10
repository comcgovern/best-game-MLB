import unittest

from mlbdelta.config import BattingWeights
from mlbdelta.scoring import (
    LeagueContext,
    batting_line_summary,
    batting_raa,
    game_score_v2,
    innings_text,
    league_context,
    pitching_line_summary,
    pitching_raa,
)


def bat(**kwargs) -> dict:
    line = {
        "pa": 0, "ab": 0, "h": 0, "b2": 0, "b3": 0, "hr": 0, "bb": 0, "ibb": 0,
        "hbp": 0, "so": 0, "sf": 0, "sh": 0, "sb": 0, "cs": 0, "rbi": 0, "r": 0,
    }
    line.update(kwargs)
    return line


def pitch(**kwargs) -> dict:
    line = {
        "outs": 0, "bf": 0, "h": 0, "r": 0, "er": 0, "bb": 0, "ibb": 0,
        "hbp": 0, "so": 0, "hr": 0, "pitches": 0, "is_start": 1,
    }
    line.update(kwargs)
    return line


CONTEXT = LeagueContext(season=2026, runs_per_pa=0.32, runs_per_out=0.166)


class BattingScoreTests(unittest.TestCase):
    def test_average_batter_scores_about_zero(self):
        """A line worth exactly the league rate must come out at zero."""
        context = LeagueContext(season=2026, runs_per_pa=0.89 / 4)
        line = bat(pa=4, ab=4, h=1)  # one single in four trips
        self.assertAlmostEqual(batting_raa(line, context), 0.0, places=9)

    def test_big_game_beats_quiet_game(self):
        big = bat(pa=5, ab=5, h=4, b2=1, hr=2, rbi=6)
        quiet = bat(pa=4, ab=4, h=1)
        self.assertGreater(batting_raa(big, CONTEXT), batting_raa(quiet, CONTEXT))

    def test_hitless_game_is_negative(self):
        self.assertLess(batting_raa(bat(pa=4, ab=4, so=3), CONTEXT), 0)

    def test_playing_time_scales_the_score(self):
        """Two homers in one game beats one homer, all else equal."""
        one = bat(pa=4, ab=4, h=1, hr=1)
        two = bat(pa=4, ab=4, h=2, hr=2)
        self.assertGreater(batting_raa(two, CONTEXT), batting_raa(one, CONTEXT))

    def test_intentional_walks_carry_no_credit(self):
        unintentional = bat(pa=4, ab=3, bb=1)
        intentional = bat(pa=4, ab=3, bb=1, ibb=1)
        self.assertGreater(
            batting_raa(unintentional, CONTEXT), batting_raa(intentional, CONTEXT)
        )

    def test_caught_stealing_costs_more_than_a_steal_gains(self):
        weights = BattingWeights()
        self.assertGreater(abs(weights.caught_stealing), weights.stolen_base)
        base = bat(pa=4, ab=4, h=1)
        self.assertGreater(batting_raa({**base, "sb": 1}, CONTEXT), batting_raa(base, CONTEXT))
        self.assertLess(batting_raa({**base, "cs": 1}, CONTEXT), batting_raa(base, CONTEXT))

    def test_summary_reads_like_a_box_score(self):
        line = bat(pa=5, ab=4, h=3, b2=1, hr=1, bb=1, rbi=3, r=2)
        self.assertEqual(batting_line_summary(line), "3-4, 2B, HR, BB, 3 RBI, 2 R")


class PitchingScoreTests(unittest.TestCase):
    def test_shutout_innings_are_positive(self):
        self.assertAlmostEqual(pitching_raa(pitch(outs=21), CONTEXT), 0.166 * 21)

    def test_runs_allowed_subtract_directly(self):
        self.assertAlmostEqual(
            pitching_raa(pitch(outs=21, r=3), CONTEXT), 0.166 * 21 - 3
        )

    def test_short_disastrous_outing_is_negative(self):
        self.assertLess(pitching_raa(pitch(outs=6, r=5), CONTEXT), -3)

    def test_length_matters_when_runs_are_equal(self):
        long_start = pitch(outs=21, r=2)
        short_start = pitch(outs=12, r=2)
        self.assertGreater(
            pitching_raa(long_start, CONTEXT), pitching_raa(short_start, CONTEXT)
        )

    def test_game_score_v2_matches_the_published_formula(self):
        # 40 + 2*27 outs + 12 K, nothing allowed = a perfect game.
        perfect = pitch(outs=27, so=12)
        self.assertEqual(game_score_v2(perfect), 40 + 54 + 12)
        rough = pitch(outs=9, h=8, r=6, bb=3, so=2, hr=2)
        self.assertEqual(game_score_v2(rough), 40 + 18 + 2 - 6 - 16 - 18 - 12)

    def test_innings_text_uses_thirds(self):
        self.assertEqual(innings_text(19), "6.1")
        self.assertEqual(innings_text(21), "7.0")

    def test_pitching_summary(self):
        line = pitch(outs=20, h=3, r=1, bb=2, so=8)
        self.assertEqual(pitching_line_summary(line), "6.2 IP, 3 H, 1 R, 2 BB, 8 K")


class LeagueContextTests(unittest.TestCase):
    def test_context_is_measured_from_the_data(self):
        batting = [bat(pa=4, ab=4, h=1) for _ in range(10)]
        pitching = [pitch(outs=27, r=5) for _ in range(10)]
        context = league_context(2026, batting, pitching)
        self.assertAlmostEqual(context.runs_per_pa, 0.89 / 4)
        self.assertAlmostEqual(context.runs_per_out, 5 / 27)
        self.assertAlmostEqual(context.runs_per_nine, 5.0)

    def test_scores_sum_to_zero_across_the_league(self):
        """The whole point of re-centring: an average performance is a zero."""
        batting = [
            bat(pa=4, ab=4, h=1),
            bat(pa=5, ab=4, h=3, hr=1, bb=1, sb=1),
            bat(pa=3, ab=3, so=2),
        ]
        pitching = [pitch(outs=21, r=2), pitch(outs=6, r=4), pitch(outs=15, r=1)]
        context = league_context(2026, batting, pitching)
        self.assertAlmostEqual(sum(batting_raa(l, context) for l in batting), 0, places=9)
        self.assertAlmostEqual(sum(pitching_raa(l, context) for l in pitching), 0, places=9)

    def test_empty_data_falls_back_instead_of_dividing_by_zero(self):
        context = league_context(2026, [], [])
        self.assertGreater(context.runs_per_pa, 0)
        self.assertGreater(context.runs_per_out, 0)


if __name__ == "__main__":
    unittest.main()
