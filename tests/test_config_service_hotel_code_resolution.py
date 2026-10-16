import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import settings
from services.config_service import config_service


def test_resolve_hotel_code_maps_entity_id_to_canonical_code(monkeypatch) -> None:
    monkeypatch.setattr(
        settings,
        "ticketing_property_entity_map_json",
        json.dumps(
            {
                "sarovar_portico_sriperumbudur": {"sriperumbudur": "6311"},
            }
        ),
        raising=False,
    )

    assert config_service.resolve_hotel_code("6311") == "sarovar_portico_sriperumbudur"
