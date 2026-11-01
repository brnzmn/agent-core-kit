---
name: explain-codebase
description: Build a codebase reading guide or walkthrough for a specified audience, using real execution paths and the user's requested example or format.
---

Infer the reader's background, immediate learning goal, and requested depth from the task. Treat a supplied example, structure, terminology, or diagram style as the output contract. Ask only for essential missing context; otherwise state a modest assumption and proceed.

Resolve the actual repository and current revision. Trace one relevant user action or data flow from entry point through the important boundaries to its result. Read the code that supports the explanation; distinguish implemented behavior from intended design or hypotheses. An export list alone does not establish runtime behavior.

Build a reading order that follows the reader's goal: the entry point, central data structures, decisive methods, and integration boundaries. Explain what each selected file contributes and what to look for there. Link verified paths and useful line locations; avoid an exhaustive file inventory.

Use a compact glossary only for unfamiliar terms required by the explanation. Include focused diagrams when they clarify a relationship, with one main flow per diagram and identifiers that match the code. Split a tangled graph instead of adding every dependency. Keep security or architectural audits outside scope unless requested or necessary to explain the behavior accurately.

Check the guide against the requested format and actual files before handing it over. Do not claim the program was executed unless it was. Make next reading steps concrete and limited to the user's learning objective, without turning a short question into a full onboarding package.
