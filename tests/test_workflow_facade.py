from services.workflow_facade import build_args, build_job_request, run_dispatch


def test_build_args_supports_free_mode_and_gui_flags() -> None:
    args = build_args(
        config='config.ini',
        project_name='demo',
        pdf_folder='D:/papers',
        summary_file='D:/subset.json',
        summary_sources=['D:/subset.json', 'D:/subset-b.json'],
        reuse_stage1=True,
        reuse_summary_files=['D:/reuse-a.json', 'D:/reuse-b.json'],
        run_all=True,
        free_mode_profile='profile.json',
        free_mode_idea='I want to compare concept A and B.',
        gui=True,
    )

    assert args.config == 'config.ini'
    assert args.project_name == 'demo'
    assert args.pdf_folder == 'D:/papers'
    assert args.summary_file == 'D:/subset.json'
    assert args.summary_sources == ['D:/subset.json', 'D:/subset-b.json']
    assert args.reuse_stage1 is True
    assert args.reuse_summary_files == ['D:/reuse-a.json', 'D:/reuse-b.json']
    assert args.run_all is True
    assert args.gui is True
    assert args.free_mode_profile == 'profile.json'
    assert args.free_mode_idea == 'I want to compare concept A and B.'


def test_build_args_supports_section_and_review_retry_flags() -> None:
    tracker = object()
    args = build_args(
        config='config.ini',
        project_name='demo',
        generate_section=2,
        retry_review_failed=True,
        progress_tracker=tracker,
    )

    assert args.generate_section == 2
    assert args.retry_review_failed is True
    assert getattr(args, '_progress_tracker') is tracker


def test_build_job_request_maps_legacy_args_to_shared_request() -> None:
    tracker = object()
    args = build_args(
        config='config.ini',
        project_name='demo',
        pdf_folder='D:/papers',
        summary_file='D:/subset.json',
        summary_sources=['D:/subset-b.json'],
        reuse_stage1=True,
        reuse_summary_files=['D:/reuse-a.json'],
        generate_review=True,
        free_mode_profile='profile.json',
        free_mode_idea='Focus on mechanism differences.',
        progress_tracker=tracker,
        gui=True,
    )

    request = build_job_request(args)

    assert request.config == 'config.ini'
    assert request.project_name == 'demo'
    assert request.pdf_folder == 'D:/papers'
    assert request.summary_file == 'D:/subset.json'
    assert request.summary_sources == ('D:/subset.json', 'D:/subset-b.json')
    assert request.reuse_stage1 is True
    assert request.reuse_summary_files == ('D:/reuse-a.json',)
    assert request.action == 'generate_review'
    assert request.generate_review is True
    assert request.free_mode_profile == 'profile.json'
    assert request.free_mode_idea == 'Focus on mechanism differences.'
    assert request.progress_tracker is tracker
    assert request.gui is True


def test_run_dispatch_accepts_queue_cancel_token(monkeypatch) -> None:
    captured = {}
    queue_token = object()

    def _fake_dispatch(args):
        captured["cancel_token"] = getattr(args, "_cancel_token", None)

    monkeypatch.setattr("main.dispatch_command", _fake_dispatch)

    result = run_dispatch(build_args(project_name="demo"), cancel_token=queue_token)

    assert result.success is True
    assert captured["cancel_token"] is queue_token
