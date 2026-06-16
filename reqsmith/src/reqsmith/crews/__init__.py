"""CrewAI crews — optional multi-agent layer (requires pip install 'reqsmith[agents]').

Import pattern in stages::

    try:
        from reqsmith.crews.drafting_crew import build_drafting_crew
        from reqsmith.crews.base import run_crew
        CREWAI_AVAILABLE = True
    except ImportError:
        CREWAI_AVAILABLE = False
"""
