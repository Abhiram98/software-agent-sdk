# Tool-composition refactoring tasks: proposals

This benchmark sits between the single-refactoring evals (`extract_method/`,
`move_code/`) and the design-pattern evals (`design_pattern_retrofits/`).
Every task can be done entirely by chaining refactorings available in the
IntelliJ IDEA GUI. Each task stands on its own; there are no paired
apply/remove directions.

## Assumptions

- **Tools:** the full IntelliJ refactoring menu is available. A high-level
  tool can be blocked when lower-level tools can reproduce it mechanically.
  Those rows are marked "decomposed", and the high-level tool is assumed
  blocked for that task.
- **Call counts:** rough guesses for a typical instance (2-3 sibling
  classes, 3-5 members or call sites). IntelliJ's Pull Up and Push Down
  dialogs are assumed to move several members in one call.
- **Pure:** "Yes" means no hand edits are needed. "Partial" means some hand
  edits remain.
- **Grading idea:** check the end state with RefactoringMiner type strings
  (as `move_code/` does) plus compile and tests. Optionally also check the
  tool-call trace to see whether the agent composed tools or edited by hand.

Background: Kim, Batory & Dig, "Scripting Refactorings in Java to Retrofit
Design Patterns" (R2): <http://dig.cs.illinois.edu/papers/ScriptingRefactorings.pdf>.
R2 had to work around three Eclipse limits: Move Method dropping parameters
it considers unused, Inline Method refusing a method that contains a
`return`, and Move Method refusing methods that call `super`. IntelliJ's
behavior on these cases has not been checked yet.

## Tasks, sorted by estimated tool calls

| ~Calls | Task | Recipe | Grows with | Pure? |
|---|---|---|---|---|
| 1 | Move instance method, trivial (control) | Move Method to a parameter or field type | — | Yes |
| 2 | Replace Temp with Query | Extract Method on the initializer, Inline Variable | — | Yes |
| 2 | Feature envy | Extract Method on the envious block, Move Method to the envied class | — | Yes |
| 2 | Change Signature, decomposed (add/remove parameter) | Introduce Parameter / Safe Delete on the parameter, Rename | parameters | Yes, except reordering parameters |
| 2 | Program to an interface | Extract Interface, Use Interface Where Possible | — | Yes |
| 2-3 | Move method whose target is not a parameter or field | Introduce Parameter for the target expression (e.g. `getFoo()` or a local), Move Method | — | Yes |
| 2-3 | Move an overriding method | Extract Method on the whole body, Move the extracted method, leave the override as a delegate | — | Yes |
| 2-3 | Move method that uses private members of the source | Move the private dependencies first (move_method / move_static_field / move_instance_field), then the method. The fork's move_method cannot widen access of members the moved method uses | private members used | Yes |
| 3 | Move method that calls `super` | Extract Method on each `super.x()` call (super delegate), Move Method | `super` calls | Yes |
| 3 | Move part of a method (split, then move) | Extract Method to split the method, Move the part that belongs elsewhere | splits | Yes |
| 3 | Hide Delegate | Extract Method on `a.getB().x()` replacing duplicates, Move Method into `A`, Rename; Find and Replace Code Duplicates for call sites in other classes | client classes | Yes |
| 3 | Pull Up with dependency | Encapsulate Field, Pull Up the getter as abstract, Pull Up the method | fields used | Yes |
| 3 | Extract Superclass, decomposed | Empty Extract Superclass, Pull Up Members | — | Yes |
| 3 | Extract Interface, decomposed | Empty Extract Interface, Pull Up Members as abstract | — | Yes |
| 3 | Inline Super Class, decomposed | Push Down all members, Safe Delete the superclass | — | Yes; check how Safe Delete handles `extends` |
| 3 | Replace Method with Method Object, decomposed | Extract Method on the whole body, Introduce Parameter Object on the extracted method, Convert to Instance Method into the new class; optionally Introduce Field per local | locals turned into fields | Yes |
| 3 | Command | Replace Method with Method Object, Extract Interface (`execute`), Rename | — | Yes |
| 3-4 | Replace Constructor with Factory Method, decomposed | Extract Method on `new Foo(..)` (inside a static method of `Foo` if one exists; otherwise Make Static and Move to `Foo`), Find and Replace Code Duplicates for other callers, Change Visibility of the constructor | constructors | Yes |
| 4 | Remove Middleman, decomposed | Inline Method on each delegating method | delegating methods | Yes; no good instance in OpenRefine (its forwarding methods wrap private maps or registries, so inlining them would not compile) |
| 4 | Strategy seam (one concrete strategy) | Extract Method, Extract Delegate (method only), Extract Interface, Introduce Parameter or Field to inject it | — | Yes |
| 4 | Data clump to a class with behavior | Introduce Parameter Object, Move Method for each method that uses it | methods moved | Yes |
| 4+ | Move Field to an existing class | Add field, Encapsulate Field, Move the accessors | — | Partial: needs an "add field" building block plus one hand edit |
| 4-6 | Move method that writes coupled fields | Move the fields (new target: Extract Delegate on fields and method; existing target: see Move Field), widen access, Move Method | coupled fields | Yes for a new target; Partial for an existing one |
| 4-6 | Move a cluster of mutually calling methods | Widen access on shared private helpers, Move Method for each, in dependency order | methods in cluster | Yes |
| 4-6 | Encapsulate Fields, decomposed | Extract Method for each getter and setter | fields | Partial: external callers by hand |
| 4-6 | Collapse Hierarchy | Pull Up, retype usages, Safe Delete the subclass | usages of the subclass | Mostly |
| 5 | Command, with Replace Method with Method Object blocked | Decomposed Method Object (above), Extract Interface, Rename | — | Partial |
| 5-8 | Replace Inheritance with Delegation, decomposed | Extract Field on `new Super()`, Extract Method for each delegating method | inherited methods used | Partial: `extends` removed by hand |
| 6-10 | Program to an interface, with Extract Interface and Use Interface Where Possible blocked | Empty Extract Interface, Pull Up, Type Migration at each usage | usages | Yes |
| 8-10 | Use Interface Where Possible, decomposed | Type Migration at each usage | usages | Yes |
| 8-10 | Factory / Abstract Factory | Replace Constructor with Factory Method for each constructor, Move, Convert to Instance Method for each, Extract Interface | constructors | Yes, with empty Extract Interface or Superclass to create the type |
| 10-15 | Form Template Method | Per sibling: Extract Method and Rename for each differing part; then Pull Up the skeleton, Safe Delete the duplicates | siblings x differing parts | Yes, if the skeletons become identical |
| 15-25 | Visitor (R2 `makeVisitor`) | Introduce Parameter (the visitor); per subclass: Extract Method, Move, two Renames | subclasses (~4 per subclass) | Yes; IntelliJ's `super` and parameter handling unverified |

## Not decomposable (kept as basic tools)

Replace Constructor with Builder, Extract Delegate, Introduce Parameter
Object, Move Inner to Upper Level, Wrap Method Return Value, Invert Boolean,
Make Static, Convert to Instance Method. These create types or code that no
other tool produces. Builder is therefore a single-tool task in IntelliJ and
belongs with the controls rather than in this benchmark.

## Open decisions

1. Building blocks: create empty types via empty Extract Interface/Superclass?
   Allow an "add field" primitive so Move Field becomes fully tool-only?
2. Partial rows: exclude them, or include them with the high-level tool left
   available?
3. Sourcing: mine real instances from history (RefactoringMiner
   co-occurrences such as "Extract and Move Method"), or seed tasks in
   OpenRefine like `move_code/`?
