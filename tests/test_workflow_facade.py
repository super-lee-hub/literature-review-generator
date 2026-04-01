from services.workflow_facade import build_args


def test_build_args_supports_free_mode_and_gui_flags() -> None:
    args = build_args(
        config='config.ini',
        project_name='demo',
        pdf_folder='D:/papers',
        run_all=True,
        free_mode_profile='profile.json',
        free_mode_idea='I want to compare concept A and B.',
        gui=True,
    )

    assert args.config == 'config.ini'
    assert args.project_name == 'demo'
    assert args.pdf_folder == 'D:/papers'
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
