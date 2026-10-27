# Pattern Inventory

These are notes on each pattern in `benchmark.json`, after the confidence
filters. The exact counts are in `BENCHMARK_SUMMARY.md`, which is regenerated
on every build. The counts below are a snapshot of 213 tasks from 39 projects.

**Terms used below:**
- *Complete*: the task has both states, so both subtasks are available.
- *Remove-only*: DPD-F instances at the latest HEAD; the apply subtask needs a
  synthesized reversal first.

## Strong coverage: complete tasks from several projects

- **Builder**: 56 tasks (53 complete) across 15 projects.
  - 10 are hand-verified tesis cases from hbase, junit4, and guava.
  - The three hbase `*MetricsBuilder` tasks from commit `448ba3a` also carry
    synthesized removal patches as `alternatives`, in telescoping-constructor
    and all-args variants.
  - 40 are only LLM-validated. These are mostly hbase, netty, camel, and
    elasticsearch.
- **Strategy**: 17 tasks, all complete, across 9 projects.
  - 5 are hand-verified: junit4 `RuleFieldValidator`, activemq `FactoryFinder`
    and `TcpTransportFactory`, and ant `FTP` (×2).
  - The jbehave case is borderline.
- **Visitor**: 43 tasks (8 complete).
  - 7 of the complete tasks come from manual reversal branches: flink (×2),
    jsoup, OpenRefine, cayenne, camunda, and jackrabbit.
  - The other 35 are DPD-F, mostly Eclipse CDT AST visitors.
- **FactoryMethod**: 23 tasks (7 complete).
  - 1 is hand-verified (junit4 `ParametersRunnerFactory`), 2 are borderline ant
    cases, and 4 are LLM-validated hbase commits.
  - `factorymethod_hbase_c61cb7f` (`RpcControllerFactory`) carries a
    synthesized switch/if-else removal patch.

## Usable but thin

- **Adapter**: 29 tasks, only 4 complete. These are ant `TaskAdapter`
  (hand-verified) plus LLM-validated camel, hadoop, and spring-boot commits.
  The other 25 are DPD-F.
- **Decorator**: 6 tasks (4 complete).
  - ant `ResourceDecorator`, which is human-validated and has prompts.
  - The flink `StreamCompressionDecorator` reversal.
  - Two LLM-validated commons-collections commits.
- **Observer**: 11 tasks (3 complete).
  - Two human-validated ant commits (`BuildListener`, `SubBuildListener`).
  - The checkstyle `AuditListener` manual reversal.
  - The other 8 are DPD-F.
- **Singleton**: 12 tasks. Only cayenne `SingletonFaultFactory` is complete.
  The 11 DPD-F tasks all come from AllBinary-Platform, which has 28 stars, so
  project diversity is poor.

## One instance only

These need more data before results on them mean much.

- **Command**: flink `AkkaInvocationHandler` (human-validated, complete).
- **Composite**: camel `CompositeHttpConfigurer` (LLM-validated, complete).
- **State**: hadoop `HAState`, from HDFS-1974 (LLM-validated, complete). The
  tesis pipeline had recorded a revert merge as the birth commit, and this was
  corrected by hand.
- **Memento**: birt `Memento` (DPD-F, remove-only).

## Remove-only patterns (DPD-F only)

- **AbstractFactory**: 7 tasks, zk and jbehave-core.
- **Facade**: 3 tasks, all jbehave-core.
- **Proxy**: 2 tasks, cdt and playorm.

The apply subtask for these patterns needs a reversal to be written first,
following the same process used for the reversal branches.

## Dropped by the confidence filters

The filters removed 201 tasks:

- **DPD-F structural check (160 tasks).** This removed every Prototype hit
  (16). Most were Liferay `LayoutPrototype*` domain classes or `*Wrapper`
  classes.
- **LLM saw no code (37 tasks).** These are tesis pipeline approvals, mostly
  netty, camel, hibernate-orm, and elasticsearch.
- **Target missing at the pattern commit (4 tasks).** These had a wrong birth
  commit or a rename that couldn't be traced.

Separately, all 4 Iterator candidates had already failed human validation.

## Not covered at all

No source has instances of Prototype, Iterator, Template Method, Chain of
Responsibility, Mediator, Bridge, Flyweight, or Interpreter.
`raw_data/MINING_SOURCES.md` lists where to look. Template Method and Chain of
Responsibility are common in frameworks such as Spring, Camel, and Netty, so
they should be the easiest to mine.

## Mining priorities

1. Patterns with no complete tasks: AbstractFactory, Facade, Proxy, Memento.
2. Patterns with a single instance: Command, Composite, State.
3. Patterns with no instances: Template Method and Chain of Responsibility
   first, then Prototype.
4. Singleton and Adapter instances, or reversal branches, from projects other
   than AllBinary, and from sources other than DPD-F.
