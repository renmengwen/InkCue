from __future__ import annotations

import unittest

from scripts.initial_approval_options import (
    InitialApprovalOptionError,
    build_initial_approval_options,
    parse_initial_approval_response,
    render_numbered_options,
)


CONTENT_IDENTITY = "a" * 64


class InitialApprovalOptionTests(unittest.TestCase):
    def test_fixed_image_mode_uses_exact_four_approval_sentences_and_one_revision(self) -> None:
        options = build_initial_approval_options(
            voiceover_mode="edge-tts",
            gpt_login_image_generation_available=False,
            configured_image_provider_available=True,
        )
        self.assertEqual(
            [option["text"] for option in options],
            [
                "草案与制作方案通过，使用 BGM，后续由 AI 自主推进至成片。",
                "草案与制作方案通过，不使用 BGM，后续由 AI 自主推进至成片。",
                "草案与制作方案通过，使用 BGM，后续由我逐阶段确认。",
                "草案与制作方案通过，不使用 BGM，后续由我逐阶段确认。",
                "草案与制作方案需要修改。修改意见：……",
            ],
        )
        self.assertTrue(all(option["imageGenerationMode"] == "provider" for option in options[:4]))
        self.assertNotIn("Edge", render_numbered_options(options))
        self.assertNotIn("MiniMax", render_numbered_options(options))
        self.assertNotIn("豆包", render_numbered_options(options))

    def test_two_real_image_capabilities_expand_only_legal_eight_approval_combinations(self) -> None:
        options = build_initial_approval_options(
            voiceover_mode="doubao",
            gpt_login_image_generation_available=True,
            configured_image_provider_available=True,
        )
        approvals = [option for option in options if option["action"] == "approve"]
        self.assertEqual(len(approvals), 8)
        self.assertEqual(len(options), 9)
        combinations = {
            (
                option["backgroundMusicEnabled"],
                option["agentApprovalEnabled"],
                option["imageGenerationMode"],
            )
            for option in approvals
        }
        self.assertEqual(
            combinations,
            {
                (bgm, agent, image)
                for bgm in (True, False)
                for agent in (True, False)
                for image in ("gpt-login", "provider")
            },
        )
        self.assertEqual(
            approvals[0]["text"],
            "草案与制作方案通过，使用 BGM，使用当前登录的 GPT 账号生成图片，后续由 AI 自主推进至成片。",
        )
        self.assertTrue(
            any("使用已配置图片供应商生成图片" in option["text"] for option in approvals)
        )

    def test_unavailable_image_combinations_are_never_shown(self) -> None:
        gpt_only = build_initial_approval_options(
            voiceover_mode="minimax",
            gpt_login_image_generation_available=True,
            configured_image_provider_available=False,
        )
        approvals = [option for option in gpt_only if option["action"] == "approve"]
        self.assertEqual(len(approvals), 4)
        self.assertTrue(all(option["imageGenerationMode"] == "gpt-login" for option in approvals))
        self.assertFalse(any("图片供应商" in option["text"] for option in approvals))
        self.assertFalse(any("GPT 账号" in option["text"] for option in approvals))

        none_available = build_initial_approval_options(
            voiceover_mode="edge-tts",
            gpt_login_image_generation_available=False,
            configured_image_provider_available=False,
        )
        self.assertEqual(
            [option["action"] for option in none_available],
            ["revise_content"],
        )

        with self.assertRaises(InitialApprovalOptionError):
            build_initial_approval_options(
                voiceover_mode="edge-tts",
                gpt_login_image_generation_available=False,
                configured_image_provider_available=True,
                fixed_image_generation_mode="gpt-login",
            )

    def test_parser_accepts_only_current_full_sentence_or_number_and_binds_content_identity(self) -> None:
        options = build_initial_approval_options(
            voiceover_mode="edge-tts",
            gpt_login_image_generation_available=False,
            configured_image_provider_available=True,
        )
        by_sentence = parse_initial_approval_response(
            options[0]["text"],
            options=options,
            content_identity_sha256=CONTENT_IDENTITY,
        )
        self.assertEqual(by_sentence["schemaVersion"], 2)
        self.assertEqual(by_sentence["contentIdentitySha256"], CONTENT_IDENTITY)
        self.assertNotIn("sampleIdentitySha256", by_sentence)
        self.assertTrue(by_sentence["readyForAtomicApproval"])
        self.assertEqual(by_sentence["matchedBy"], "full_sentence")

        by_number = parse_initial_approval_response(
            "2",
            options=options,
            content_identity_sha256=CONTENT_IDENTITY,
        )
        self.assertEqual(by_number["choiceId"], options[1]["choiceId"])
        self.assertEqual(by_number["matchedBy"], "number")

        for invalid in (
            "草案和制作方案都通过，AI 继续吧",
            "使用 BGM / 不使用 BGM",
            "12",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(InitialApprovalOptionError):
                    parse_initial_approval_response(
                        invalid,
                        options=options,
                        content_identity_sha256=CONTENT_IDENTITY,
                    )
        with self.assertRaises(InitialApprovalOptionError):
            parse_initial_approval_response(
                "1",
                options=options,
                content_identity_sha256="invalid",
            )

    def test_revision_prefix_is_deterministic_and_does_not_guess_free_text(self) -> None:
        options = build_initial_approval_options(
            voiceover_mode="edge-tts",
            gpt_login_image_generation_available=False,
            configured_image_provider_available=True,
        )
        result = parse_initial_approval_response(
            "草案与制作方案需要修改。修改意见：调整第二幕的表达。",
            options=options,
            content_identity_sha256=CONTENT_IDENTITY,
        )
        self.assertEqual(result["action"], "revise_content")
        self.assertEqual(result["revisionInstructions"], "调整第二幕的表达。")
        self.assertFalse(result["readyForAtomicApproval"])

    def test_disabled_srt_keeps_bgm_off_and_uses_content_only_identity(self) -> None:
        options = build_initial_approval_options(
            voiceover_mode="disabled",
            gpt_login_image_generation_available=False,
            configured_image_provider_available=True,
        )
        approvals = [option for option in options if option["action"] == "approve"]
        self.assertEqual(len(approvals), 2)
        self.assertTrue(all(option["text"].startswith("字幕与分镜方案通过") for option in approvals))
        self.assertTrue(all(option["backgroundMusicEnabled"] is False for option in approvals))
        result = parse_initial_approval_response(
            "1",
            options=options,
            content_identity_sha256=CONTENT_IDENTITY,
        )
        self.assertEqual(result["contentIdentitySha256"], CONTENT_IDENTITY)
        self.assertNotIn("sampleIdentitySha256", result)


if __name__ == "__main__":
    unittest.main()
