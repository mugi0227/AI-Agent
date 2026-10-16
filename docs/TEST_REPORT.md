# 納品ソースの実行テスト結果

## Chrome・Edge対応時の追試（2026-10-16）

- Linux / Python 3.12.14 / Node 24.19.0で68件のunittest、compileall、架空デモ、拡張JS構文検査、Power Automateドラフト生成を再実行し、すべて成功。
- 初回GitHub CIはLinuxのPython3.11/3.13で成功。Windowsの同2構成では、テスト内の`Path.write_text/read_text`で文字コードを省略したため、日本語データの4試験が失敗。該当箇所にUTF-8を明示する修正を追加。
- Windows CIへ`tests/test-native-host.ps1`を追加。Chrome・Edgeを別々に登録し、C#→分離モードのPython→Ping/日本語capture、未許可ID拒否、Edge解除後のChrome接続を試験する。実ブラウザUIや実テナントの確認は含まない。
- 以降の各コミットのWindows/Linux実行結果は[GitHub Actions](https://github.com/mugi0227/AI-Agent/actions)を参照。

以下は初版ZIP作成時の記録であり、現在のGitHubへの反映状況・CI実行状況とは区別する。

## 初版ZIPの検証記録

実施日: 2026-10-16。環境: Linux / Python 3.13.5.

**この結果はローカル/モック/架空データによるものです。Windows、AWS実接続、M365/Boxテナント、GitHub CIは未実行です。**

## python -m unittest discover -s tests -v

結果: PASS

```text
test_call_id_collision (test_core.AgentTests.test_call_id_collision) ... ok
test_cancellation (test_core.AgentTests.test_cancellation) ... ok
test_model_incomplete (test_core.AgentTests.test_model_incomplete) ... ok
test_no_approval_tool (test_core.AgentTests.test_no_approval_tool) ... ok
test_no_notify_in_chat (test_core.AgentTests.test_no_notify_in_chat) ... ok
test_read_before_propose (test_core.AgentTests.test_read_before_propose) ... ok
test_reasoning_and_call_id_roundtrip (test_core.AgentTests.test_reasoning_and_call_id_roundtrip) ... ok
test_round_limit (test_core.AgentTests.test_round_limit) ... ok
test_runtime_offline (test_core.AgentTests.test_runtime_offline) ... ok
test_strict_tool_arguments (test_core.AgentTests.test_strict_tool_arguments) ... ok
test_unknown_tool_not_executed (test_core.AgentTests.test_unknown_tool_not_executed) ... ok
test_unverified_citation_blocked (test_core.AgentTests.test_unverified_citation_blocked) ... ok
test_docx_extract (test_core.IngestionTests.test_docx_extract) ... ok
test_drop_stability_and_retry_fixed_file (test_core.IngestionTests.test_drop_stability_and_retry_fixed_file) ... ok
test_file_refresh_after_change (test_core.IngestionTests.test_file_refresh_after_change) ... ok
test_html_removes_active_content (test_core.IngestionTests.test_html_removes_active_content) ... ok
test_live_file_revalidation (test_core.IngestionTests.test_live_file_revalidation) ... ok
test_path_outside (test_core.IngestionTests.test_path_outside) ... ok
test_secret_name (test_core.IngestionTests.test_secret_name) ... ok
test_state_blocked (test_core.IngestionTests.test_state_blocked) ... ok
test_symlink_blocked (test_core.IngestionTests.test_symlink_blocked) ... ok
test_vtt_speakers_and_times (test_core.IngestionTests.test_vtt_speakers_and_times) ... ok
test_xml_entities_rejected (test_core.IngestionTests.test_xml_entities_rejected) ... ok
test_business_hours (test_core.ScheduleTests.test_business_hours) ... ok
test_foreground_yields_monitor (test_core.ScheduleTests.test_foreground_yields_monitor) ... ok
test_no_change_no_inference (test_core.ScheduleTests.test_no_change_no_inference) ... ok
test_notification_cooldown (test_core.ScheduleTests.test_notification_cooldown) ... ok
test_notification_dedupe (test_core.ScheduleTests.test_notification_dedupe) ... ok
test_overnight_weekday (test_core.ScheduleTests.test_overnight_weekday) ... ok
test_persistent_claim (test_core.ScheduleTests.test_persistent_claim) ... ok
test_quiet_hours (test_core.ScheduleTests.test_quiet_hours) ... ok
test_weekend (test_core.ScheduleTests.test_weekend) ... ok
test_atomic_budget (test_core.StorageTests.test_atomic_budget) ... ok
test_audit_no_body (test_core.StorageTests.test_audit_no_body) ... ok
test_calendar_requires_interval (test_core.StorageTests.test_calendar_requires_interval) ... ok
test_clock_requires_timezone (test_core.StorageTests.test_clock_requires_timezone) ... ok
test_collision (test_core.StorageTests.test_collision) ... ok
test_completed_task_not_revived (test_core.StorageTests.test_completed_task_not_revived) ... ok
test_default_off (test_core.StorageTests.test_default_off) ... ok
test_delete_before_arrival (test_core.StorageTests.test_delete_before_arrival) ... ok
test_deleted_cannot_resurrect_out_of_order (test_core.StorageTests.test_deleted_cannot_resurrect_out_of_order) ... ok
test_duplicate_receipt (test_core.StorageTests.test_duplicate_receipt) ... ok
test_expiry_removes_derived (test_core.StorageTests.test_expiry_removes_derived) ... ok
test_fake_quote_blocked (test_core.StorageTests.test_fake_quote_blocked) ... ok
test_fts_query_is_data (test_core.StorageTests.test_fts_query_is_data) ... ok
test_japanese_search (test_core.StorageTests.test_japanese_search) ... ok
test_new_authorized_observation_after_revocation (test_core.StorageTests.test_new_authorized_observation_after_revocation) ... ok
test_not_approved (test_core.StorageTests.test_not_approved) ... ok
test_old_revision (test_core.StorageTests.test_old_revision) ... ok
test_proposal_approval (test_core.StorageTests.test_proposal_approval) ... ok
test_reserved_metadata (test_core.StorageTests.test_reserved_metadata) ... ok
test_bridge_allowlist (test_core.TransportTests.test_bridge_allowlist) ... ok
test_bridge_duplicate_and_no_retained_body (test_core.TransportTests.test_bridge_duplicate_and_no_retained_body) ... ok
test_bridge_unknown_id (test_core.TransportTests.test_bridge_unknown_id) ... ok
test_capture_disallowed_host (test_core.TransportTests.test_capture_disallowed_host) ... ok
test_capture_disallowed_origin (test_core.TransportTests.test_capture_disallowed_origin) ... ok
test_capture_url_redaction (test_core.TransportTests.test_capture_url_redaction) ... ok
test_cross_region_explicit (test_core.TransportTests.test_cross_region_explicit) ... ok
test_endpoint_openai_blocked (test_core.TransportTests.test_endpoint_openai_blocked) ... ok
test_endpoint_suffix_attack (test_core.TransportTests.test_endpoint_suffix_attack) ... ok
test_mantle_special_path (test_core.TransportTests.test_mantle_special_path) ... ok
test_native_binary_unicode (test_core.TransportTests.test_native_binary_unicode) ... ok
test_native_fragmented_stream (test_core.TransportTests.test_native_fragmented_stream) ... ok
test_native_length_guard (test_core.TransportTests.test_native_length_guard) ... ok
test_runtime_profile_required (test_core.TransportTests.test_runtime_profile_required) ... ok
test_sse_delta_and_completion (test_core.TransportTests.test_sse_delta_and_completion) ... ok
test_sse_truncation (test_core.TransportTests.test_sse_truncation) ... ok
test_wire_store_false (test_core.TransportTests.test_wire_store_false) ... ok

----------------------------------------------------------------------
Ran 68 tests in 0.281s

OK

```

## python -m compileall -q work_assistant

結果: PASS

```text

```

## node --check extension/popup.js

結果: PASS

```text

```

## python -m work_assistant demo

結果: PASS

```text
{
  "mode": "offline_synthetic_demo",
  "results": [
    {
      "chunk_id": "6afd7859-9c30-5d54-971e-0e8515b87d09:0",
      "text": "田中: 塗工幅を測定しました。\n佐藤: 次回までに測定条件を確認します。",
      "id": "6afd7859-9c30-5d54-971e-0e8515b87d09",
      "title": "架空データ: 塗工条件の検討",
      "kind": "transcript",
      "source_url": "https://example.invalid/demo",
      "modified_at": "2026-10-16T04:53:01.163448+00:00",
      "observed_at": "2026-10-16T04:53:01.163448+00:00",
      "expires_at": "2026-10-17T04:53:01.163448+00:00",
      "rank": -5.124999999999999e-06
    }
  ]
}

```

## python power_automate/build_transcript_draft.py --sink onedrive --output /mnt/data/pa-review-only-draft.json

結果: PASS

```text

```

## xvfb-run -a python <synthetic GUI smoke>

結果: PASS

```text
PASS: Linux/Xvfb GUI launch, six tabs, Japanese search, close (not Windows validation)

```

## python -m pip wheel --no-index --no-deps --no-build-isolation .

結果: PASS

```text
Processing <source-root>
  Preparing metadata (pyproject.toml): started
  Preparing metadata (pyproject.toml): finished with status 'done'
Building wheels for collected packages: work-assistant
  Building wheel for work-assistant (pyproject.toml): started
  Building wheel for work-assistant (pyproject.toml): finished with status 'done'
  Created wheel for work-assistant: filename=work_assistant-0.1.0-py3-none-any.whl size=48980 sha256=4242fdf7531a8b0b0fca96ed358adcd8dfe43c7d3919a91c938a9b6e4579d0be
  Stored in directory: /tmp/pip-ephem-wheel-cache-5g0n52bf/wheels/70/41/13/862a3e9a530d27701c470ffd45ff3dac9cb42aff85e5b126ad
Successfully built work-assistant
WARNING: The directory '/home/oai/.cache/pip' or its parent directory is not owned or is not writable by the current user. The cache has been disabled. Check the permissions and owner of that directory. If executing pip with sudo, you should use sudo's -H flag.

```

## 再実行

ZIP展開先のAI-Agentで `python -m unittest discover -s tests -v` を実行してください。会社PCでの判定はCOMPANY_CODEX_PROMPT.mdに従ってください。
