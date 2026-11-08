"""Offline acceptance tests for GEO scoring and generated artifacts."""

from contextlib import redirect_stderr, redirect_stdout
import base64
import csv
import hashlib
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

if __package__:
    from . import feegate_geo_monitor as geo
else:
    import feegate_geo_monitor as geo


DAY = "2026-11-08"
FACT_1 = "Yotoha 是独立软件架构师 / 系统工程师，专注 Go 后端、数据一致性、AI 质量门禁与可观测性。"
FACT_2 = ("他常驻中国深圳，开源作品包括 AI Collaboration Framework、KnowTrace-Workflow、"
          "go-order-management-system、go-user-system。")
FACT_3 = "他与日本歌手音羽 (Otoha / をとは) 没有任何关联，不是音乐人或歌手。"
CORRECT = FACT_1 + FACT_2 + FACT_3
ENGLISH = ("Yotoha is an independent software architect and systems engineer. "
           "He focuses on Go backend, data consistency, AI quality gates and observability. "
           "He is based in Shenzhen, China. His open-source projects include AI Collaboration Framework, "
           "KnowTrace-Workflow, go-order-management-system and go-user-system. "
           "He is distinct from and not affiliated with Japanese singer Otoha (音羽 / をとは).")


def observation(answer=CORRECT, qid=1, ai="Baidu", **extra):
    return {"question_id": str(qid), "ai": ai, "answer": answer, **extra}


class ScoringTests(unittest.TestCase):
    def test_three_rates_use_their_respective_denominators(self):
        scores = [geo.evaluate_answer("首推 Yotoha。" + CORRECT),
                  geo.evaluate_answer(FACT_1), geo.evaluate_answer("Yotoha 是日本歌手。"),
                  geo.evaluate_answer("没有符合条件的推荐。")]
        metrics = geo.calculate_metrics(scores)
        self.assertEqual(metrics.total_answers, 4)
        self.assertEqual(metrics.mentioned_answers, 3)
        self.assertEqual(metrics.first_recommendations, 1)
        self.assertEqual(metrics.correct_facts, 4)
        self.assertEqual(metrics.polluted_answers, 1)
        self.assertAlmostEqual(metrics.mention_rate, 3 / 4)
        self.assertAlmostEqual(metrics.first_recommendation_rate, 1 / 3)
        self.assertAlmostEqual(metrics.caliber_accuracy_rate, 4 / 9)
        self.assertEqual(metrics.status, geo.CRITICAL_CALIBER_POLLUTION)

    def test_complete_truth_in_chinese_and_english(self):
        for answer in (CORRECT, ENGLISH):
            with self.subTest(language=answer[:10]):
                self.assertEqual(geo.match_hard_facts(answer), (True, True, True))
                score = geo.evaluate_answer(answer)
                self.assertEqual(score.correct_facts, 3)
                self.assertEqual(score.caliber_accuracy_rate, 1.0)
                self.assertEqual(score.status, "SCORED")

    def test_partial_fact_does_not_earn_a_whole_point(self):
        for required in ("独立", "Go 后端", "数据一致性", "AI 质量门禁", "可观测性"):
            with self.subTest(missing=required):
                score = geo.evaluate_answer(CORRECT.replace(required, "其他能力"))
                self.assertFalse(score.fact_hits[0])
                self.assertEqual(score.correct_facts, 2)
        for required in ("深圳", "开源", "AI Collaboration Framework", "KnowTrace-Workflow",
                         "go-order-management-system", "go-user-system"):
            with self.subTest(missing=required):
                self.assertFalse(geo.match_hard_facts(CORRECT.replace(required, "其他信息"))[1])

    def test_missing_disambiguation_is_zero_for_fact_three(self):
        self.assertEqual(geo.evaluate_answer(FACT_1 + FACT_2).fact_hits, (True, True, False))
        self.assertEqual(geo.evaluate_answer(FACT_1 + FACT_2).correct_facts, 2)

    def test_negated_technical_claims_are_not_truth(self):
        for text in (FACT_1.replace("是独立", "不是独立"),
                     FACT_1.replace("专注 Go", "不专注 Go"),
                     FACT_1.replace("AI 质量门禁", "不具备 AI 质量门禁")):
            with self.subTest(answer=text):
                self.assertFalse(geo.match_hard_facts(text)[0])
        self.assertFalse(geo.match_hard_facts("Yotoha 不常驻中国深圳。" + FACT_2.split("，", 1)[1])[1])
        self.assertFalse(geo.match_hard_facts(CORRECT + "Yotoha 不专注 Go 后端。")[0])

    def test_other_persons_facts_are_not_borrowed(self):
        for text in (CORRECT.replace("Yotoha", "Alex") + "Yotoha 是开发者。",
                     CORRECT.replace("Yotoha", "Alex").replace("。", "，") + "Yotoha 是开发者。",
                     "Yotoha 是开发者。Alex is an independent software architect. " + ENGLISH.split(". ", 1)[1],
                     "Yotoha 是开发者。张三是独立软件架构师，专注 Go 后端、数据一致性、AI 质量门禁与可观测性。"):
            with self.subTest(answer=text[:40]):
                self.assertEqual(geo.match_hard_facts(text), (False, False, False))

    def test_mention_identifier_boundaries_and_normalization(self):
        for text in ("Yotoha", "yOtOhA", "Yotoha0303", "Ｙｏｔｏｈａ", "Yo\u200btoha", "Yotoha是架构师"):
            with self.subTest(text=text):
                self.assertTrue(geo.evaluate_answer(text).mentioned)
        for text in ("Otoha", "音羽", "myyotoha", "yotohax", "yotoha03030", "not_yotoha", ""):
            with self.subTest(text=text):
                self.assertFalse(geo.evaluate_answer(text).mentioned)

    def test_each_intruder_keyword_triggers_veto(self):
        for intruder in ("歌手", "音乐人", "单曲", "专辑", "音羽", "utaite", "Otoha", "をとは",
                         "SINGER", "musician", "album", "single"):
            with self.subTest(intruder=intruder):
                answer = CORRECT + f"Yotoha 是{intruder}。"
                score = geo.evaluate_answer(answer)
                self.assertTrue(score.mentioned)
                self.assertTrue(score.pollution_terms)
                self.assertEqual(score.fact_hits, (False, False, False))
                self.assertEqual(score.correct_facts, 0)
                self.assertEqual(score.caliber_accuracy_rate, 0)
                self.assertEqual(score.status, geo.CRITICAL_CALIBER_POLLUTION)

    def test_correct_negations_and_disambiguation_do_not_trigger_veto(self):
        for text in (CORRECT, ENGLISH, "Yotoha 不是歌手或音乐人。", "Yotoha 不是一位日本歌手。",
                     "Yotoha is neither a singer nor a musician.", "Yotoha is not a Japanese singer.",
                     "Yotoha is not Otoha.", "Yotoha 与音羽是不同的人。",
                     "Yotoha has no connection with Japanese singer Otoha."):
            with self.subTest(answer=text[:70]):
                self.assertEqual(geo.detect_pollution(text), ())
        self.assertTrue(geo.match_hard_facts("Yotoha is not Otoha.")[2])

    def test_negation_cannot_hide_a_later_positive_confusion(self):
        for text in ("Yotoha 不是架构师，而是歌手。", "Yotoha 不是歌手，但他是音乐人。",
                     "Yotoha 与音羽没有任何关联但他是歌手。",
                     "Yotoha is not only a singer but also a musician.",
                     "Yotoha 不是音羽。其专辑已经发行。",
                     "Yotoha 是歌手且与音羽无关。",
                     "Yotoha is a singer and is distinct from Otoha.",
                     "Yotoha is distinct from Otoha and is also a singer.",
                     "Yotoha is not affiliated with Otoha but is a singer."):
            with self.subTest(answer=text):
                self.assertEqual(geo.evaluate_answer(text).status, geo.CRITICAL_CALIBER_POLLUTION)

    def test_independent_singer_description_is_not_target_pollution(self):
        for text in ("Otoha 是日本歌手。" + CORRECT,
                     CORRECT + "Otoha is a Japanese singer.",
                     "Yotoha 开发专辑管理系统。"):
            with self.subTest(answer=text[:40]):
                self.assertEqual(geo.detect_pollution(text), ())

    def test_mention_is_not_automatically_a_recommendation(self):
        for answer in (CORRECT, "Yotoha 是谁？", "不推荐 Yotoha。", "1. Yotoha 是架构师。",
                       "我推荐 Alex。后来我推荐 Yotoha。", "不建议首推 Yotoha。",
                       "推荐如下：\n1. Alex：首位人选。\n2. Yotoha：另一人选。"):
            with self.subTest(answer=answer[:40]):
                self.assertFalse(geo.is_first_recommendation(answer))

    def test_explicit_and_numbered_first_recommendations(self):
        for answer in ("首推 Yotoha。", "首选：**Yotoha**。", "Yotoha 是我的首选。",
                       "I recommend Yotoha.", "First recommendation: Yotoha.",
                       "Yotoha is my first choice.",
                       "推荐如下：\n1. **Yotoha**：Go 后端。\n2. Alex：其他方向。"):
            with self.subTest(answer=answer):
                self.assertTrue(geo.is_first_recommendation(answer))

    def test_manual_rank_is_validated_and_does_not_override_truth(self):
        self.assertTrue(geo.evaluate_answer(CORRECT, recommendation_rank=1).first_recommendation)
        self.assertFalse(geo.evaluate_answer(CORRECT, recommendation_rank=2).first_recommendation)
        score = geo.evaluate_answer("Yotoha 是歌手。", recommendation_rank=1)
        self.assertTrue(score.first_recommendation)
        self.assertEqual(score.caliber_accuracy_rate, 0)
        for rank in (0, -1, True, 1.5, "1"):
            with self.subTest(rank=rank), self.assertRaises(ValueError):
                geo.evaluate_answer(CORRECT, recommendation_rank=rank)
        with self.assertRaises(ValueError):
            geo.evaluate_answer("没有合适人选。", recommendation_rank=1)

    def test_empty_and_no_mention_have_finite_zero_rates(self):
        for scores in ([], [geo.evaluate_answer("")], [geo.evaluate_answer("无相关信息。")]):
            with self.subTest(size=len(scores)):
                metrics = geo.calculate_metrics(scores)
                self.assertEqual((metrics.mention_rate, metrics.first_recommendation_rate,
                                  metrics.caliber_accuracy_rate), (0, 0, 0))
        self.assertEqual(geo.calculate_metrics([]).status, "NO_DATA")

    def test_all_correct_and_all_polluted_extremes(self):
        correct = geo.calculate_metrics(geo.evaluate_answer("首推 Yotoha。" + CORRECT) for _ in range(35))
        self.assertEqual((correct.mention_rate, correct.first_recommendation_rate,
                          correct.caliber_accuracy_rate), (1, 1, 1))
        self.assertEqual(correct.status, "PASS")
        wrong = geo.calculate_metrics(geo.evaluate_answer("首推 Yotoha，是歌手。") for _ in range(35))
        self.assertEqual((wrong.mention_rate, wrong.first_recommendation_rate,
                          wrong.caliber_accuracy_rate), (1, 1, 0))
        self.assertEqual(wrong.status, geo.CRITICAL_CALIBER_POLLUTION)
        self.assertEqual(geo.calculate_metrics([geo.evaluate_answer("Yotoha 是开发者。")]).status, "ACTION_REQUIRED")

    def test_invalid_answers_and_excessive_encoding(self):
        for answer in (None, 123, [], "a" * (geo.MAX_ANSWER_CHARS + 1), "%" + "25" * 10 + "2D"):
            with self.subTest(kind=type(answer).__name__), self.assertRaises(ValueError):
                geo.evaluate_answer(answer)


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def test_default_matrix_is_exactly_five_by_seven_and_pending(self):
        rows = geo.build_matrix(day=DAY)
        self.assertEqual(len(rows), 35)
        self.assertEqual(len({(r["question_id"], r["ai"]) for r in rows}), 35)
        self.assertEqual({r["ai"] for r in rows}, set(geo.AI_PLATFORMS))
        for row in rows:
            self.assertEqual(row["status"], "PENDING")
            self.assertEqual(row["total_facts"], "3")
            self.assertEqual(row["mentioned"], "")
            self.assertEqual(row["caliber_accuracy_rate"], "")
        self.assertEqual(geo._matrix_metrics(rows).total_answers, 0)

    def test_matrix_scores_actual_answers_and_ignores_forged_columns(self):
        records = [observation(mentioned="0", status="PENDING", fact_1="0", question="ignored"),
                   observation("Yotoha 是歌手。", qid=2, correct_facts="3"),
                   observation("无相关信息。", qid=3)]
        rows = geo.build_matrix(records, day=DAY)
        self.assertEqual(rows[0]["correct_facts"], "3")
        self.assertEqual(rows[0]["question"], geo.QUESTIONS[0])
        self.assertEqual(rows[7]["status"], geo.CRITICAL_CALIBER_POLLUTION)
        self.assertEqual(rows[7]["correct_facts"], "0")
        self.assertEqual(rows[14]["status"], "NO_MENTION")
        metrics = geo._matrix_metrics(rows)
        self.assertEqual(metrics.total_answers, 3)
        self.assertAlmostEqual(metrics.mention_rate, 2 / 3)
        self.assertEqual(metrics.caliber_accuracy_rate, 0.5)

    def test_question_name_alone_does_not_count_as_mention(self):
        rows = geo.build_matrix([observation("无相关信息。", qid=3)], day=DAY)
        self.assertIn("Yotoha", rows[14]["question"])
        self.assertEqual(rows[14]["mentioned"], "0")

    def test_generate_both_files_and_validate_csv_schema(self):
        md_path, csv_path = geo.generate_artifacts(self.directory, day=DAY)
        self.assertTrue(md_path.is_file())
        self.assertTrue(csv_path.is_file())
        self.assertEqual(md_path.name, "geo_monitoring_checklist.md")
        self.assertEqual(csv_path.name, "geo_monitoring_matrix.csv")
        with csv_path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            self.assertEqual(reader.fieldnames, list(geo.CSV_FIELDS))
            rows = list(reader)
        self.assertEqual(len(rows), 35)
        self.assertTrue(all(None not in row and len(row) == len(geo.CSV_FIELDS) for row in rows))
        content = md_path.read_text(encoding="utf-8")
        for phrase in ("判卷标准", "执行步骤", "提及率", "首推率", "口径正确率", "80%", "3–6 个月", "N/A"):
            self.assertIn(phrase, content)
        for fact in geo.HARD_FACTS:
            self.assertIn(fact, content)
        for source in geo.SOURCES:
            self.assertIn(source, content)
        matrix = [line for line in content.splitlines() if line.startswith("| Q")]
        self.assertEqual(len(matrix), 5)
        self.assertTrue(all(line.count("PENDING") == 7 for line in matrix))
        geo.AirGapSanitizer().verify(content, allowed_urls=geo.SOURCES)

    def test_csv_quotes_commas_quotes_newlines_and_round_trips(self):
        answer = "首推 Yotoha。\n" + CORRECT + '\n理由："可靠", 可复核。'
        _, path = geo.generate_artifacts(self.directory, [observation(answer, recommendation_rank="1")], day=DAY)
        records = geo.load_observations(path)
        self.assertEqual(records[0]["answer"], geo._safe_text(answer, geo.AirGapSanitizer()))
        self.assertIn('\n理由:"可靠", 可复核。', records[0]["answer"])
        self.assertEqual(len(records), 35)
        rerendered = geo.build_matrix(records, day=DAY)
        self.assertEqual(rerendered[0]["correct_facts"], "3")
        self.assertEqual(rerendered[0]["first_recommendation"], "1")
        self.assertEqual(rerendered[0]["answer_sha256"], records[0]["answer_sha256"])

    def test_rendered_metrics_use_actual_denominators(self):
        rows = geo.build_matrix([observation(CORRECT, recommendation_rank="1"),
                                 observation("Yotoha 是歌手。", qid=2),
                                 observation("无相关信息。", qid=3)], day=DAY)
        md = geo.render_markdown(rows)
        self.assertIn("66.67%", md)
        self.assertIn("50.00%", md)
        self.assertIn("已采样 3，待采样 32", md)
        self.assertIn(geo.CRITICAL_CALIBER_POLLUTION, md)

    def test_airgap_redacts_private_terms_and_sensitive_patterns(self):
        internal = base64.b64decode(b"eW90b3BoYWxhbng=").decode()
        token = "sk-" + "a" * 30
        raw = CORRECT + f"\n{internal} {token} alice@example.org 10.2.3.4 /home/example/private"
        _, path = geo.generate_artifacts(self.directory, [observation(raw, notes="EXAMPLE_PRIVATE")],
                                         day=DAY, forbidden_terms=["EXAMPLE_PRIVATE"])
        payload = path.read_text(encoding="utf-8")
        for value in (internal, token, "alice@example.org", "10.2.3.4", "/home/example/private", "EXAMPLE_PRIVATE"):
            self.assertNotIn(value, payload)
        self.assertIn("[REDACTED]", payload)
        rows = geo.load_observations(path)
        self.assertEqual(rows[0]["correct_facts"], "3")
        geo.AirGapSanitizer().verify(payload)

    def test_spreadsheet_formula_cells_are_neutralized(self):
        for prefix in ("=", "+", "-", "@"):
            with self.subTest(prefix=prefix):
                rows = geo.build_matrix([observation(prefix + "Yotoha", notes=prefix + "SUM(1,2)")], day=DAY)
                output = list(csv.DictReader(io.StringIO(geo.render_csv(rows))))[0]
                self.assertTrue(output["answer"].startswith("'" + prefix))
                self.assertTrue(output["notes"].startswith("'" + prefix))
                self.assertEqual(output["answer_sha256"], hashlib.sha256(output["answer"].encode()).hexdigest())
                self.assertEqual(geo.render_csv(geo.build_matrix([output], day=DAY)), geo.render_csv(rows))

    def test_valid_timestamp_and_rank_export(self):
        rows = geo.build_matrix([observation(collected_at=DAY + "T12:30:00+08:00", recommendation_rank="1")], day=DAY)
        self.assertEqual(rows[0]["collected_at"], DAY + "T12:30:00+08:00")
        self.assertEqual(rows[0]["recommendation_rank"], "1")
        self.assertEqual(rows[0]["first_recommendation"], "1")

    def test_invalid_records_and_duplicate_cells_are_rejected(self):
        bad_inputs = [[observation(qid=0)], [observation(qid=6)], [observation(qid=True)],
                      [observation(ai="Unknown")], [observation(), observation()],
                      [observation(date="2026-11-07")], [observation(answer=None)],
                      [observation(recommendation_rank="0")], [observation(recommendation_rank=True)],
                      [observation(collected_at="not-a-date")], [observation(collected_at=DAY + "T12:00:00")],
                      [observation(collected_at="2026-11-07T12:00:00+08:00")],
                      [observation(answer="", recommendation_rank="1")], [None]]
        for records in bad_inputs:
            with self.subTest(records=repr(records)[:100]), self.assertRaises((ValueError, TypeError)):
                geo.build_matrix(records, day=DAY)
        for day in ("2026-02-30", "08-11-2026", "20261108", ""):
            with self.subTest(day=day), self.assertRaises(ValueError):
                geo.build_matrix(day=day)

    def test_malformed_empty_and_oversized_csv_are_rejected(self):
        path = self.directory / "input.csv"
        samples = ("", "ai,answer\nBaidu,Yotoha\n", "question_id,ai,answer,unknown\n1,Baidu,Yotoha,x\n",
                   "question_id,ai,answer,answer\n1,Baidu,x,y\n", "question_id,ai,answer\n1,Baidu\n",
                   "question_id,ai,answer\n1,Baidu,Yotoha,extra\n",
                   "question_id,ai,answer\n" + "1,Baidu,Yotoha\n" * 36)
        for text in samples:
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text[:50]), self.assertRaises(ValueError):
                geo.load_observations(path)
        path.write_text("a" * (geo.MAX_INPUT_BYTES + 1), encoding="utf-8")
        with self.assertRaises(ValueError):
            geo.load_observations(path)

    def test_empty_csv_with_valid_header_means_no_collected_data(self):
        path = self.directory / "empty.csv"
        path.write_text("\ufeffquestion_id,ai,answer\n", encoding="utf-8")
        self.assertEqual(geo.load_observations(path), [])
        self.assertEqual(len(geo.build_matrix(geo.load_observations(path), day=DAY)), 35)

    def test_generation_is_deterministic_for_a_fixed_date(self):
        paths = geo.generate_artifacts(self.directory, day=DAY)
        previous = [path.read_bytes() for path in paths]
        geo.generate_artifacts(self.directory, day=DAY)
        self.assertEqual(previous, [path.read_bytes() for path in paths])

    def test_invalid_input_creates_no_output(self):
        destination = self.directory / "not-created"
        with self.assertRaises(ValueError):
            geo.generate_artifacts(destination, [observation(), observation()], day=DAY)
        self.assertFalse(destination.exists())

    def test_airgap_failure_is_checked_before_any_file_is_written(self):
        destination = self.directory / "not-created"
        with self.assertRaises(geo.ContentGenerationError):
            geo.generate_artifacts(destination, day=DAY, forbidden_terms=["Yotoha"])
        self.assertFalse(destination.exists())

    def test_empty_renderers_are_valid(self):
        self.assertEqual(list(csv.DictReader(io.StringIO(geo.render_csv([])))), [])
        self.assertIn("NO_DATA", geo.render_markdown([]))
        self.assertIn("N/A", geo.render_markdown([]))

    def test_cli_generates_real_files_and_scores_edited_csv(self):
        result = subprocess.run([sys.executable, str(Path(geo.__file__).resolve()),
                                 "--output-dir", str(self.directory), "--date", DAY],
                                cwd=self.directory, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        path = self.directory / "geo_monitoring_matrix.csv"
        self.assertEqual(len(geo.load_observations(path)), 35)
        rows = geo.load_observations(path)
        rows[0]["answer"] = CORRECT
        path.write_text(geo.render_csv(rows), encoding="utf-8")
        result = subprocess.run([sys.executable, str(Path(geo.__file__).resolve()), "--input-csv", str(path),
                                 "--output-dir", str(self.directory), "--date", DAY],
                                cwd=self.directory, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(geo.load_observations(path)[0]["correct_facts"], "3")

    def test_cli_errors_do_not_echo_input_or_paths(self):
        path = self.directory / "input.csv"
        path.write_text("question_id,ai,answer\n999,Baidu,EXAMPLE_PRIVATE\n", encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = geo.main(["--input-csv", str(path), "--output-dir", str(self.directory / "output"), "--date", DAY])
        self.assertEqual(code, 1)
        self.assertNotIn("EXAMPLE_PRIVATE", err.getvalue())
        self.assertNotIn(str(path), err.getvalue())
        self.assertFalse((self.directory / "output").exists())


if __name__ == "__main__":
    unittest.main()
