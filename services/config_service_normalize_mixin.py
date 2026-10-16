"""ConfigServiceNormalizeMixin: methods extracted from config_service.py (behavior-preserving).

Moved verbatim (18 methods). ConfigService inherits this mixin, so ``self.*``
resolution is unchanged. This module never imports config_service — no cycle.
"""
import copy
import re
from datetime import UTC, datetime
from typing import Optional, Dict, Any
from uuid import uuid4



class ConfigServiceNormalizeMixin:
    @staticmethod
    def _normalize_identifier(value: Any) -> str:
        """Normalize IDs to stable lowercase snake-style identifiers."""
        return str(value or "").strip().lower().replace(" ", "_")

    @classmethod
    def _normalize_phase_identifier(cls, value: Any) -> str:
        """Normalize phase IDs and map legacy aliases to canonical values."""
        normalized = cls._normalize_identifier(value)
        aliases = {
            "prebooking": "pre_booking",
            "booking": "pre_checkin",
            "precheckin": "pre_checkin",
            "duringstay": "during_stay",
            "instay": "during_stay",
            "in_stay": "during_stay",
            "postcheckout": "post_checkout",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _normalize_slug(value: Any) -> str:
        """Normalize free-form text to a stable slug."""
        lowered = str(value or "").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", lowered).strip("_")
        return slug

    @classmethod
    def _normalize_channel_identifier(cls, value: Any) -> str:
        """Normalize channel IDs to canonical runtime values."""
        normalized = cls._normalize_identifier(value)
        if normalized in {"web", "web_widget", "widget", "chat_widget"}:
            return "web"
        if normalized in {"wa", "whatsapp", "whats_app"}:
            return "whatsapp"
        return normalized

    @classmethod
    def _normalize_service_prompt_pack(
        cls,
        pack: Any,
        *,
        service: Dict[str, Any],
        source: str = "manual_override",
    ) -> Dict[str, Any]:
        generated = cls._generate_service_prompt_pack(service)
        if not isinstance(pack, dict):
            return generated

        normalized = copy.deepcopy(generated)
        for key in ("role", "professional_behavior", "profile"):
            value = str(pack.get(key) or "").strip()
            if value:
                normalized[key] = value

        # If the saved pack has ticketing_conditions, the admin has explicitly defined
        # what to collect via that text. Wipe the auto-generated default slots so they
        # don't override the admin's intent.
        if str(pack.get("ticketing_conditions") or "").strip():
            normalized["required_slots"] = []

        required_slots_raw = pack.get("required_slots")
        if isinstance(required_slots_raw, list):
            normalized_slots: list[dict[str, Any]] = []
            seen_slot_ids: set[str] = set()
            for item in required_slots_raw:
                normalized_slot = cls._coerce_service_prompt_slot(item)
                if not normalized_slot:
                    continue
                slot_id = normalized_slot["id"]
                if slot_id in seen_slot_ids:
                    continue
                seen_slot_ids.add(slot_id)
                normalized_slots.append(normalized_slot)
            if normalized_slots:
                normalized["required_slots"] = normalized_slots
                normalized["validation_rules"] = cls._default_service_prompt_validation_rules(normalized_slots)

        validation_rules_raw = pack.get("validation_rules")
        if isinstance(validation_rules_raw, list):
            normalized_rules: list[dict[str, Any]] = []
            for rule in validation_rules_raw:
                if not isinstance(rule, dict):
                    continue
                rule_id = cls._normalize_identifier(rule.get("id"))
                rule_type = cls._normalize_identifier(rule.get("type"))
                if not rule_id or not rule_type:
                    continue
                slot_ids_raw = rule.get("slot_ids", [])
                if not isinstance(slot_ids_raw, list):
                    slot_ids_raw = []
                slot_ids = [cls._normalize_identifier(slot_id) for slot_id in slot_ids_raw]
                slot_ids = [slot_id for slot_id in slot_ids if slot_id]
                normalized_rule: Dict[str, Any] = {
                    "id": rule_id,
                    "type": rule_type,
                    "slot_ids": slot_ids,
                    "error_message": str(rule.get("error_message") or "").strip(),
                }
                if "min_value" in rule:
                    try:
                        normalized_rule["min_value"] = float(rule.get("min_value"))
                    except (TypeError, ValueError):
                        pass
                normalized_rules.append(normalized_rule)
            if normalized_rules:
                normalized["validation_rules"] = normalized_rules

        confirmation = pack.get("confirmation_format")
        if isinstance(confirmation, dict):
            normalized_confirmation = dict(normalized.get("confirmation_format", {}))
            style_value = str(confirmation.get("style") or "").strip()
            template_value = str(confirmation.get("template") or "").strip()
            phrase_value = str(confirmation.get("required_phrase") or "").strip()
            if style_value:
                normalized_confirmation["style"] = style_value
            if template_value:
                normalized_confirmation["template"] = template_value
            if phrase_value:
                normalized_confirmation["required_phrase"] = phrase_value
            normalized["confirmation_format"] = normalized_confirmation

        ticketing = pack.get("ticketing_policy")
        if isinstance(ticketing, dict):
            normalized_ticketing = dict(normalized.get("ticketing_policy", {}))
            if "enabled" in ticketing:
                normalized_ticketing["enabled"] = bool(ticketing.get("enabled"))
            policy_value = str(ticketing.get("policy") or "").strip()
            if policy_value:
                normalized_ticketing["policy"] = policy_value
            decision_template = str(ticketing.get("decision_template") or "").strip()
            if decision_template:
                normalized_ticketing["decision_template"] = decision_template
            normalized["ticketing_policy"] = normalized_ticketing

        execution_guard = pack.get("execution_guard")
        if isinstance(execution_guard, dict):
            normalized_guard = dict(normalized.get("execution_guard", {}))
            if "require_required_slots_before_confirm" in execution_guard:
                normalized_guard["require_required_slots_before_confirm"] = bool(
                    execution_guard.get("require_required_slots_before_confirm")
                )
            normalized["execution_guard"] = normalized_guard

        # Preserve free-text fields that are not part of structural validation
        passthrough: dict[str, str] = {}
        for passthrough_key in ("extracted_knowledge", "ticketing_conditions"):
            value = str(pack.get(passthrough_key) or "").strip()
            if value:
                normalized[passthrough_key] = value
                passthrough[passthrough_key] = value

        try:
            normalized["version"] = max(1, int(pack.get("version") or normalized.get("version") or 1))
        except (TypeError, ValueError):
            normalized["version"] = 1
        normalized["source"] = str(source or "manual_override")
        if not cls._is_valid_service_prompt_pack(normalized):
            # Still carry passthrough fields even when falling back to generated defaults
            generated.update(passthrough)
            return generated
        return normalized

    @classmethod
    def _normalize_service_entry(
        cls,
        service: Dict[str, Any],
        *,
        manual_prompt_override: bool = False,
        preserve_manual_prompt_pack: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one service entry from admin inputs."""
        if not isinstance(service, dict):
            return None

        service_id = cls._normalize_identifier(service.get("id"))
        if not service_id:
            return None

        normalized = dict(service)
        normalized["id"] = service_id
        normalized["name"] = str(service.get("name") or service_id).strip()
        normalized["type"] = cls._normalize_identifier(service.get("type") or "service")
        normalized["description"] = str(service.get("description") or "").strip()
        normalized["cuisine"] = str(service.get("cuisine") or "").strip() or None
        phase_id = cls._normalize_phase_identifier(service.get("phase_id"))
        if phase_id:
            normalized["phase_id"] = phase_id
        elif "phase_id" in normalized:
            normalized.pop("phase_id", None)
        if "ticketing_enabled" in service:
            normalized["ticketing_enabled"] = bool(service.get("ticketing_enabled"))
        normalized["is_builtin"] = bool(service.get("is_builtin", False))
        hours_value = service.get("hours")
        if isinstance(hours_value, dict):
            open_value = str(hours_value.get("open") or "").strip()
            close_value = str(hours_value.get("close") or "").strip()
            normalized["hours"] = {"open": open_value, "close": close_value} if (open_value or close_value) else {}
        else:
            normalized["hours"] = {}

        delivery_zones_value = service.get("delivery_zones")
        normalized_zones: list[str] = []
        if isinstance(delivery_zones_value, list):
            for zone in delivery_zones_value:
                zone_str = str(zone or "").strip().lower()
                if zone_str and zone_str not in normalized_zones:
                    normalized_zones.append(zone_str)
        elif isinstance(delivery_zones_value, str):
            zone_str = delivery_zones_value.strip().lower()
            if zone_str:
                normalized_zones = [zone_str]
        normalized["delivery_zones"] = normalized_zones
        normalized["is_active"] = bool(service.get("is_active", True))
        normalized["ticketing_policy"] = str(service.get("ticketing_policy") or "").strip()

        # --- ticketing_mode & form_config ---
        # ticketing_mode: "form" | "text" | "none"
        raw_ticketing_mode = str(service.get("ticketing_mode") or "").strip().lower()
        if raw_ticketing_mode in ("form", "text", "none"):
            normalized["ticketing_mode"] = raw_ticketing_mode
        else:
            # Backward compat: derive from ticketing_enabled
            normalized["ticketing_mode"] = "text" if normalized.get("ticketing_enabled") else "none"

        form_config = service.get("form_config")
        if isinstance(form_config, dict):
            # Normalize form_config
            norm_fc: Dict[str, Any] = {}
            # trigger_field
            tf = form_config.get("trigger_field")
            if isinstance(tf, dict) and tf.get("id"):
                norm_fc["trigger_field"] = {
                    "id": str(tf["id"]).strip(),
                    "label": str(tf.get("label") or tf["id"]).strip(),
                    "description": str(tf.get("description") or "").strip(),
                }
            # fields
            raw_fields = form_config.get("fields")
            if isinstance(raw_fields, list):
                norm_fields = []
                for f in raw_fields:
                    if not isinstance(f, dict):
                        continue
                    fid = str(f.get("id") or "").strip()
                    if not fid:
                        continue
                    norm_field = {
                        "id": fid,
                        "label": str(f.get("label") or fid).strip(),
                        "type": str(f.get("type") or "text").strip(),
                        "required": bool(f.get("required", True)),
                        "validation_prompt": str(f.get("validation_prompt") or "").strip(),
                    }
                    # Carry through optional per-field metadata: select options,
                    # prefill sources, and deterministic date/time slot constraints.
                    # Purely additive — these keys are absent on most services, so
                    # behaviour is unchanged unless a service explicitly sets them.
                    _opts = f.get("options")
                    if isinstance(_opts, list):
                        _clean_opts = [str(o).strip() for o in _opts if str(o or "").strip()]
                        if _clean_opts:
                            norm_field["options"] = _clean_opts
                    for _passthru in (
                        "prefill_source", "default_value", "description", "placeholder",
                        "slot_source_field", "slot_hours", "slot_step_minutes",
                    ):
                        _v = f.get(_passthru)
                        if _v not in (None, ""):
                            norm_field[_passthru] = _v
                    _shm = f.get("slot_hours_map")
                    if isinstance(_shm, dict) and _shm:
                        norm_field["slot_hours_map"] = _shm
                    norm_fields.append(norm_field)
                norm_fc["fields"] = norm_fields
            else:
                norm_fc["fields"] = []
            norm_fc["pre_form_instructions"] = str(form_config.get("pre_form_instructions") or "").strip()
            # Preserve Lumira-specific keys so the form trigger can set render/render_type
            for _lumira_key in ("lumira_form_key", "lumira_entity_id"):
                _val = str(form_config.get(_lumira_key) or "").strip()
                if _val:
                    norm_fc[_lumira_key] = _val
            normalized["form_config"] = norm_fc
        elif "form_config" not in normalized:
            # Only set default if not already present on the service
            if normalized["ticketing_mode"] == "form":
                normalized["form_config"] = {"trigger_field": {}, "fields": [], "pre_form_instructions": ""}
            # For text/none modes, don't add form_config to keep config clean

        existing_prompt_pack = service.get("service_prompt_pack")
        existing_source = ""
        if isinstance(existing_prompt_pack, dict):
            existing_source = str(existing_prompt_pack.get("source") or "").strip().lower()
        existing_custom_flag = bool(service.get("service_prompt_pack_custom", False))
        existing_manual = existing_custom_flag or existing_source in {
            "manual_override",
            "admin_ui",
            "admin_override",
            "db",
        }

        prompt_pack: Dict[str, Any]
        if manual_prompt_override and isinstance(existing_prompt_pack, dict):
            prompt_pack = cls._normalize_service_prompt_pack(
                existing_prompt_pack,
                service=normalized,
                source="manual_override",
            )
            prompt_pack_custom = True
        elif preserve_manual_prompt_pack and existing_manual and isinstance(existing_prompt_pack, dict):
            prompt_pack = cls._normalize_service_prompt_pack(
                existing_prompt_pack,
                service=normalized,
                source="manual_override",
            )
            prompt_pack_custom = True
        else:
            prompt_pack = cls._generate_service_prompt_pack(normalized)
            prompt_pack_custom = False

        if not cls._is_valid_service_prompt_pack(prompt_pack):
            prompt_pack = cls._generate_service_prompt_pack(normalized)
            prompt_pack_custom = False
        if str(prompt_pack.get("source") or "").strip().lower() != "manual_override":
            prompt_pack_custom = False
        normalized["service_prompt_pack"] = prompt_pack
        normalized["service_prompt_pack_custom"] = prompt_pack_custom
        return normalized

    @classmethod
    def _normalize_phase_entry(cls, phase: Dict[str, Any], default_order: int = 0) -> Optional[Dict[str, Any]]:
        """Normalize one phase entry from admin inputs."""
        if not isinstance(phase, dict):
            return None

        raw_phase_id = cls._normalize_identifier(phase.get("id"))
        phase_id = cls._normalize_phase_identifier(phase.get("id"))
        if not phase_id:
            return None

        name = str(phase.get("name") or phase_id.replace("_", " ").title()).strip()
        description = str(phase.get("description") or "").strip()
        if raw_phase_id == "booking":
            if not str(phase.get("name") or "").strip() or str(phase.get("name") or "").strip().lower() == "booking":
                name = "Pre Checkin"
            if not description or "reservation/payment/modify/cancel" in description.lower():
                description = "Guest booking is confirmed and needs support before arrival."
        try:
            order_value = int(phase.get("order", default_order))
        except Exception:
            order_value = int(default_order)

        return {
            "id": phase_id,
            "name": name,
            "description": description,
            "is_active": bool(phase.get("is_active", True)),
            "order": order_value,
        }

    @classmethod
    def _normalize_intent_entry(cls, intent: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one intent entry from admin inputs."""
        if not isinstance(intent, dict):
            return None

        intent_id = cls._normalize_identifier(intent.get("id"))
        if not intent_id:
            return None

        normalized: Dict[str, Any] = {
            "id": intent_id,
            "label": str(intent.get("label") or intent_id.replace("_", " ").title()).strip(),
            "enabled": bool(intent.get("enabled", True)),
        }
        maps_to = cls._normalize_identifier(intent.get("maps_to"))
        if maps_to and maps_to != intent_id:
            normalized["maps_to"] = maps_to
        return normalized

    @classmethod
    def _normalize_faq_entry(cls, faq: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one FAQ entry from admin inputs."""
        if not isinstance(faq, dict):
            return None

        question = str(faq.get("question") or "").strip()
        answer = str(faq.get("answer") or "").strip()
        if not question or not answer:
            return None

        faq_id = cls._normalize_identifier(faq.get("id")) or cls._normalize_slug(question)
        if not faq_id:
            return None

        tags = faq.get("tags", [])
        if not isinstance(tags, list):
            tags = []

        normalized_tags = []
        for tag in tags:
            normalized_tag = cls._normalize_slug(tag)
            if normalized_tag and normalized_tag not in normalized_tags:
                normalized_tags.append(normalized_tag)

        # Property scope: empty = COMMON to all properties under the hotel; a
        # slug (e.g. "zone_park_jammu") scopes the FAQ to that one property. The
        # runtime (faq_context_service) hides other-property FAQs when a property
        # is focused. property_id (the new_bot_properties row id) is carried for
        # the admin UI's edit/round-trip; the runtime keys off property_slug.
        property_slug = cls._normalize_identifier(
            faq.get("property_slug") or faq.get("property_scope_id")
        )
        property_id_raw = faq.get("property_id")
        try:
            property_id = int(property_id_raw) if str(property_id_raw or "").strip() else None
        except (TypeError, ValueError):
            property_id = None

        return {
            "id": faq_id,
            "question": question,
            "answer": answer,
            "description": str(faq.get("description") or "").strip(),
            "tags": normalized_tags,
            "enabled": bool(faq.get("enabled", True)),
            "property_slug": property_slug,
            "property_id": property_id,
        }

    @classmethod
    def _normalize_ticketing_cases(cls, raw_cases: Any) -> list[str]:
        """Normalize ticketing-case rows from either strings or object entries."""
        if not isinstance(raw_cases, list):
            return []

        cleaned: list[str] = []
        for item in raw_cases:
            if isinstance(item, dict):
                text = str(item.get("description") or item.get("case") or item.get("label") or "").strip()
            else:
                text = str(item or "").strip()
            if not text:
                continue
            normalized = re.sub(r"\s+", " ", text)
            if normalized and normalized not in cleaned:
                cleaned.append(normalized)
        return cleaned[:40]

    @classmethod
    def _normalize_tool_entry(cls, tool: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one tool entry from admin inputs."""
        if not isinstance(tool, dict):
            return None

        tool_id = cls._normalize_identifier(tool.get("id")) or cls._normalize_slug(tool.get("name"))
        if not tool_id:
            return None

        channels = tool.get("channels", [])
        if not isinstance(channels, list):
            channels = []
        normalized_channels = []
        for channel in channels:
            channel_id = cls._normalize_identifier(channel)
            if channel_id and channel_id not in normalized_channels:
                normalized_channels.append(channel_id)

        normalized_tool: Dict[str, Any] = {
            "id": tool_id,
            "name": str(tool.get("name") or tool_id.replace("_", " ").title()).strip(),
            "description": str(tool.get("description") or "").strip(),
            "type": cls._normalize_identifier(tool.get("type") or "workflow"),
            "handler": str(tool.get("handler") or "").strip() or None,
            "channels": normalized_channels,
            "enabled": bool(tool.get("enabled", True)),
            "requires_confirmation": bool(tool.get("requires_confirmation", False)),
        }
        if "ticketing_plugin_enabled" in tool:
            normalized_tool["ticketing_plugin_enabled"] = bool(tool.get("ticketing_plugin_enabled", True))

        ticketing_cases = cls._normalize_ticketing_cases(tool.get("ticketing_cases"))
        if ticketing_cases:
            normalized_tool["ticketing_cases"] = ticketing_cases

        return normalized_tool

    @classmethod
    def _normalize_agent_slot_entry(cls, slot: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one slot definition used by service-agent plugins."""
        if not isinstance(slot, dict):
            return None

        slot_id = cls._normalize_identifier(slot.get("id")) or cls._normalize_slug(slot.get("name"))
        if not slot_id:
            return None

        prompt = str(slot.get("prompt") or "").strip()
        if not prompt:
            prompt = f"Please share {slot_id.replace('_', ' ')}."

        label = str(slot.get("label") or slot_id.replace("_", " ").title()).strip()

        return {
            "id": slot_id,
            "label": label,
            "prompt": prompt,
            "required": bool(slot.get("required", True)),
        }

    @classmethod
    def _normalize_agent_fact_entry(cls, fact: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one fact entry used by service-agent plugins."""
        if not isinstance(fact, dict):
            return None

        text = str(fact.get("text") or "").strip()
        if not text:
            return None

        fact_id = cls._normalize_identifier(fact.get("id"))
        if not fact_id:
            fact_id = f"fact_{uuid4().hex[:10]}"

        source = str(fact.get("source") or "").strip()
        origin = cls._normalize_identifier(fact.get("origin") or "")
        if origin not in {"manual", "auto", "menu_ocr"}:
            origin = "manual" if source.startswith("manual") else "auto"

        tags_value = fact.get("tags", [])
        if isinstance(tags_value, str):
            tags_value = [part.strip() for part in tags_value.split(",")]
        if not isinstance(tags_value, list):
            tags_value = []
        normalized_tags: list[str] = []
        for tag in tags_value:
            normalized_tag = cls._normalize_slug(tag)
            if normalized_tag and normalized_tag not in normalized_tags:
                normalized_tags.append(normalized_tag)

        status = cls._normalize_identifier(fact.get("status") or "pending")
        if status not in {"pending", "approved", "rejected"}:
            status = "pending"

        created_at = str(fact.get("created_at") or datetime.now(UTC).isoformat()).strip()
        updated_at = str(fact.get("updated_at") or created_at).strip()
        approved_by = str(fact.get("approved_by") or "").strip()
        approved_at = str(fact.get("approved_at") or "").strip()
        confidence_raw = fact.get("confidence")
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(confidence, 1.0))
        evidence = str(fact.get("evidence") or "").strip()
        if status == "approved":
            if not approved_at:
                approved_at = datetime.now(UTC).isoformat()
        else:
            approved_by = ""
            approved_at = ""

        return {
            "id": fact_id,
            "text": text,
            "source": source,
            "origin": origin,
            "tags": normalized_tags,
            "status": status,
            "approved_by": approved_by or None,
            "approved_at": approved_at or None,
            "created_at": created_at,
            "updated_at": updated_at,
            "confidence": confidence,
            "evidence": evidence or None,
        }

    @classmethod
    def _normalize_agent_plugin_entry(cls, plugin: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one service-agent plugin entry."""
        if not isinstance(plugin, dict):
            return None

        plugin_id = cls._normalize_identifier(plugin.get("id"))
        if not plugin_id:
            return None

        slot_schema_value = plugin.get("slot_schema", [])
        if not isinstance(slot_schema_value, list):
            slot_schema_value = []
        normalized_slots: list[Dict[str, Any]] = []
        seen_slots: set[str] = set()
        for slot in slot_schema_value:
            normalized_slot = cls._normalize_agent_slot_entry(slot)
            if not normalized_slot:
                continue
            slot_id = normalized_slot["id"]
            if slot_id in seen_slots:
                continue
            seen_slots.add(slot_id)
            normalized_slots.append(normalized_slot)

        trigger_phrases_value = plugin.get("trigger_phrases", [])
        if not isinstance(trigger_phrases_value, list):
            trigger_phrases_value = []
        normalized_triggers: list[str] = []
        for phrase in trigger_phrases_value:
            phrase_text = str(phrase or "").strip().lower()
            if phrase_text and phrase_text not in normalized_triggers:
                normalized_triggers.append(phrase_text)

        channels_value = plugin.get("channels", [])
        if not isinstance(channels_value, list):
            channels_value = []
        normalized_channels: list[str] = []
        for channel in channels_value:
            channel_id = cls._normalize_channel_identifier(channel)
            if channel_id and channel_id not in normalized_channels:
                normalized_channels.append(channel_id)

        response_templates = plugin.get("response_templates", {})
        if not isinstance(response_templates, dict):
            response_templates = {}
        normalized_templates = {}
        for key in ("intro", "confirmation", "success", "cancelled", "fallback"):
            value = response_templates.get(key)
            if value is None:
                continue
            normalized_templates[key] = str(value).strip()

        knowledge_scope = plugin.get("knowledge_scope", {})
        if not isinstance(knowledge_scope, dict):
            knowledge_scope = {}
        keywords = knowledge_scope.get("keywords", [])
        if not isinstance(keywords, list):
            keywords = []
        normalized_keywords: list[str] = []
        for keyword in keywords:
            keyword_value = str(keyword or "").strip().lower()
            if keyword_value and keyword_value not in normalized_keywords:
                normalized_keywords.append(keyword_value)

        tool_bindings = plugin.get("tool_bindings", [])
        if not isinstance(tool_bindings, list):
            tool_bindings = []

        facts_value = plugin.get("knowledge_facts", [])
        if isinstance(facts_value, str):
            parsed_facts: list[dict[str, Any]] = []
            for line in [row.strip() for row in facts_value.splitlines() if row.strip()]:
                if "|" in line:
                    left, right = line.split("|", 1)
                    parsed_facts.append({"text": left.strip(), "source": right.strip()})
                else:
                    parsed_facts.append({"text": line})
            facts_value = parsed_facts
        if not isinstance(facts_value, list):
            facts_value = []
        normalized_facts: list[dict[str, Any]] = []
        seen_fact_ids: set[str] = set()
        for fact in facts_value:
            normalized_fact = cls._normalize_agent_fact_entry(fact)
            if not normalized_fact:
                continue
            fact_id = normalized_fact["id"]
            if fact_id in seen_fact_ids:
                continue
            seen_fact_ids.add(fact_id)
            normalized_facts.append(normalized_fact)

        metadata = plugin.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        service_category = cls._normalize_identifier(plugin.get("service_category") or "transactional")
        if service_category not in {"informational", "transactional", "hybrid"}:
            service_category = "transactional"

        service_id = cls._normalize_identifier(plugin.get("service_id"))
        industry = cls._normalize_identifier(plugin.get("industry") or "custom")

        name = str(plugin.get("name") or plugin_id.replace("_", " ").title()).strip()
        description = str(plugin.get("description") or "").strip()

        if not normalized_triggers:
            normalized_triggers = [
                name.lower(),
                plugin_id.replace("_", " "),
            ]
            if service_id:
                normalized_triggers.append(service_id.replace("_", " "))

        return {
            "id": plugin_id,
            "name": name,
            "industry": industry,
            "service_id": service_id or None,
            "service_category": service_category,
            "description": description,
            "trigger_phrases": normalized_triggers,
            "slot_schema": normalized_slots,
            "confirmation_required": bool(plugin.get("confirmation_required", True)),
            "channels": normalized_channels or ["web", "whatsapp"],
            "is_active": bool(plugin.get("is_active", True)),
            "response_templates": normalized_templates,
            "knowledge_scope": {"keywords": normalized_keywords},
            "knowledge_facts": normalized_facts,
            "strict_facts_only": bool(plugin.get("strict_facts_only", True)),
            "tool_bindings": tool_bindings,
            "metadata": metadata,
            "version": int(plugin.get("version") or 1),
        }

    @classmethod
    def _normalize_service_kb_menu_document(
        cls,
        document: Dict[str, Any],
        index: int = 0,
    ) -> Optional[Dict[str, Any]]:
        """Normalize one OCR menu document artifact stored under service_kb."""
        if not isinstance(document, dict):
            return None

        doc_id = cls._normalize_identifier(document.get("id"))
        if not doc_id:
            doc_id = f"menu_doc_{index + 1}_{uuid4().hex[:8]}"

        summary = document.get("summary", {})
        if not isinstance(summary, dict):
            summary = {}

        trace = document.get("trace", {})
        if not isinstance(trace, dict):
            trace = {}

        ocr_raw_output = document.get("ocr_raw_output", {})
        if not isinstance(ocr_raw_output, dict):
            ocr_raw_output = {}

        fact_lines_value = document.get("fact_lines", [])
        if not isinstance(fact_lines_value, list):
            fact_lines_value = []
        fact_lines: list[str] = []
        seen_fact_lines: set[str] = set()
        for line in fact_lines_value:
            text = re.sub(r"\s+", " ", str(line or "")).strip()
            if not text:
                continue
            dedupe_key = text.lower()
            if dedupe_key in seen_fact_lines:
                continue
            seen_fact_lines.add(dedupe_key)
            fact_lines.append(text)

        menu_name = str(
            document.get("menu_name")
            or summary.get("menu_name")
            or ""
        ).strip()
        source_file = str(
            document.get("source_file")
            or document.get("file_name")
            or ""
        ).strip()

        return {
            "id": doc_id,
            "menu_name": menu_name or None,
            "source_file": source_file or None,
            "scanned_at": str(document.get("scanned_at") or datetime.now(UTC).isoformat()).strip(),
            "summary": summary,
            "trace": trace,
            "fact_lines": fact_lines,
            "ocr_raw_output": ocr_raw_output,
            "ocr_raw_output_text": str(document.get("ocr_raw_output_text") or "").strip(),
            "formatted_text": str(document.get("formatted_text") or "").strip(),
            "raw_text": str(document.get("raw_text") or "").strip(),
        }

    @classmethod
    def _normalize_service_kb_record(cls, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Normalize one service KB record scoped to a service/plugin."""
        if not isinstance(record, dict):
            return None

        service_id = cls._normalize_identifier(record.get("service_id"))
        plugin_id = cls._normalize_identifier(record.get("plugin_id"))
        if not service_id and not plugin_id:
            return None

        kb_id = cls._normalize_identifier(record.get("id"))
        if not kb_id:
            kb_id = f"{service_id or plugin_id}_kb"

        facts_value = record.get("facts", [])
        if not isinstance(facts_value, list):
            facts_value = []
        normalized_facts: list[dict[str, Any]] = []
        seen_fact_ids: set[str] = set()
        for fact in facts_value:
            normalized_fact = cls._normalize_agent_fact_entry(fact)
            if not normalized_fact:
                continue
            fact_id = normalized_fact["id"]
            if fact_id in seen_fact_ids:
                continue
            seen_fact_ids.add(fact_id)
            normalized_facts.append(normalized_fact)

        menu_documents_value = record.get("menu_documents", [])
        if not isinstance(menu_documents_value, list):
            menu_documents_value = []
        normalized_menu_documents: list[dict[str, Any]] = []
        seen_menu_doc_ids: set[str] = set()
        for idx, item in enumerate(menu_documents_value):
            normalized_doc = cls._normalize_service_kb_menu_document(item, index=idx)
            if not normalized_doc:
                continue
            doc_id = cls._normalize_identifier(normalized_doc.get("id"))
            if doc_id in seen_menu_doc_ids:
                continue
            seen_menu_doc_ids.add(doc_id)
            normalized_menu_documents.append(normalized_doc)

        return {
            "id": kb_id,
            "service_id": service_id or None,
            "plugin_id": plugin_id or None,
            "strict_mode": bool(record.get("strict_mode", True)),
            "facts": normalized_facts,
            "menu_documents": normalized_menu_documents,
            "version": int(record.get("version") or 1),
            "is_active": bool(record.get("is_active", True)),
            "published_at": str(record.get("published_at") or datetime.now(UTC).isoformat()).strip(),
            "published_by": str(record.get("published_by") or "").strip() or None,
            "release_notes": str(record.get("release_notes") or "").strip(),
            "completeness": record.get("completeness", {}) if isinstance(record.get("completeness"), dict) else {},
            "extracted_knowledge": str(record.get("extracted_knowledge") or "").strip(),
            "generated_extraction_prompt": str(record.get("generated_extraction_prompt") or "").strip(),
        }

    def _normalize_property_scope_index(
        self,
        payload: Any,
        *,
        source_signature: str,
        generated_at: str,
        fallback_common_source_names: list[str] | None = None,
    ) -> dict[str, Any]:
        fallback_common_source_names = fallback_common_source_names or []
        rows = payload.get("properties", []) if isinstance(payload, dict) else []
        properties: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            property_id = self._normalize_slug(row.get("id") or row.get("name"))
            if not property_id or property_id in seen_ids:
                continue
            seen_ids.add(property_id)
            name = re.sub(r"\s+", " ", str(row.get("name") or property_id.replace("_", " ").title()).strip())
            aliases: list[str] = []
            for alias in row.get("aliases", []) if isinstance(row.get("aliases"), list) else []:
                alias_clean = re.sub(r"\s+", " ", str(alias or "").strip())
                if alias_clean and alias_clean.lower() != name.lower() and alias_clean not in aliases:
                    aliases.append(alias_clean)
            source_names: list[str] = []
            for source_name in row.get("source_names", []) if isinstance(row.get("source_names"), list) else []:
                source_clean = str(source_name or "").strip()
                if source_clean and source_clean not in source_names:
                    source_names.append(source_clean)
            location = self._coerce_property_location(row.get("location"))
            if not location:
                location = self._infer_property_location_heuristic(
                    name=name,
                    aliases=aliases,
                )
            entry: dict[str, Any] = {
                "id": property_id,
                "name": name,
                "aliases": aliases,
                "source_names": source_names,
                "page_ids": [],
            }
            if location:
                entry["location"] = location
            properties.append(entry)
        common_sources_value = payload.get("common_source_names", []) if isinstance(payload, dict) else []
        common_source_names: list[str] = []
        for source_name in common_sources_value if isinstance(common_sources_value, list) else fallback_common_source_names:
            source_clean = str(source_name or "").strip()
            if source_clean and source_clean not in common_source_names:
                common_source_names.append(source_clean)
        return {
            "version": "v1",
            "generator": "llm_v1",
            "generated_at": generated_at,
            "source_signature": source_signature,
            "expected_property_count": self._coerce_expected_property_count(
                self.get_knowledge_config().get("expected_property_count")
            ),
            "properties": properties,
            "common_page_ids": [],
            "common_source_names": common_source_names,
        }

    @staticmethod
    def _normalize_delivery_zones(delivery_zones: Any) -> list[str]:
        if not isinstance(delivery_zones, list):
            return []
        normalized: list[str] = []
        for zone in delivery_zones:
            zone_text = str(zone or "").strip().lower()
            if zone_text and zone_text not in normalized:
                normalized.append(zone_text)
        return normalized
