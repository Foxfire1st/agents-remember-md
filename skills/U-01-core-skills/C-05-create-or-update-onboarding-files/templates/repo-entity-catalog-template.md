# Entities

| Field       | Value                 |
| ----------- | --------------------- |
| repository  | <repo-name>           |
| doc_type    | `repo-entity-catalog` |
| lastUpdated | <YYYY-MM-DDThh:mm>    |
| status      | <draft                |active>

## Purpose

<What this file is for and what it is not. Keep it focused on real entities and their cross-layer projections.>

## Entity Inventory

### <Canonical Entity Name>

| Field                        | Value                                                                       |
| ---------------------------- | --------------------------------------------------------------------------- |
| Category                     | <physical device part                                                       |firmware concept|configuration concept|telemetry concept|backend projection|frontend projection|other>
| Represents In Reality        | <What concrete thing or stable system object this entity stands for>        |
| Description                  | <Short description>                                                         |
| Canonical Source Of Truth    | <Which layer or model currently defines the entity most authoritatively>    |
| Current Naming Drift         | <Current synonyms, overloaded names, or misleading labels in code>          |
| Key Identifiers              | <identifiers such as ident, deviceId, address, interfaceIndex, componentId> |
| Parent / Child Relationships | <Important containment or ownership relations>                              |
| Often Confused With          | <Nearby entities it is commonly mixed up with>                              |
| Source References            | <Primary code or doc references>                                            |
| Migration Notes              | <Only if there is active drift or deferred renaming>                        |

## Cross-Layer Projections

### <Canonical Entity Name>

| Layer    | Representation                                       |
| -------- | ---------------------------------------------------- |
| Reality  | <real-world object or concept>                       |
| Firmware | <firmware-facing representation>                     |
| Backend  | <backend model, mapper, DTO, or view-model presence> |
| Frontend | <store, type, or UI projection>                      |

## Ownership Notes

<Boundaries, naming caveats, or catalog-level notes that do not belong to one entity entry. Omit when empty.>

## Update History

<!-- newest first -->
