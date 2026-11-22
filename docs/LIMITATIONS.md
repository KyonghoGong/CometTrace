# Interpretation limits

- Verification covers the retained Comet 148 and 156 data. A future format may require new evidence handlers. An unfamiliar latest backend marker can yield `unknown`. A short `first_answer` alone can stay unavailable even though an explicit short `answer_preview` is supported.
- Local artifacts establish what was recorded. A tool invocation, a completion statement, a browser History row and a verified file on disk support different conclusions. History corroboration requires a unique name and time-window match within the selected stage.
- The free-text filename extractor targets PDF. Quoted and clearly labelled names can preserve Unicode, case, spaces and repeated extensions. Unquoted prose boundaries can be ambiguous. A reference PDF can appear among candidates even when the reported download name is unavailable. History provides independent structured filename evidence.
- A missing external LevelDB blob can make a record unreadable. Preserve the diagnostic and the absence of a value. Deletion markers and surviving data may coexist.
- Context identifiers can cover multiple turns. Shared-list prompts and sidebar previews retain their source scopes. UI selection and remote action success may be impossible to reconstruct.
- Run simultaneous acquisitions in independent CLI processes. Nested synchronous handler scopes restore their previous bindings. Overlapping calls into the frozen and patched engines in one Python process are unsupported.
- Work from an authorized copy of the acquisition and preserve the original. JSON and HTML can reproduce private source text, identifiers, paths and browsing history. Keep these reports out of the public repository.
