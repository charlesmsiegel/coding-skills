# Idiom swaps

Before/after pairs. Each removes a real hazard, not just characters — the ones
that are purely cosmetic are marked as such.

## Nullish handling

```ts
if (x && x.y && x.y.z) …         →  if (x?.y?.z) …
const n = opts.count || 10;       →  const n = opts.count ?? 10;      // `||` eats 0
const s = opts.name || "";        →  const s = opts.name ?? "";       // `||` eats ""
if (x !== null && x !== undefined) → if (x != null)                   // the one legitimate `==`
obj && obj.fn && obj.fn()         →  obj?.fn?.()
arr && arr[0]                     →  arr?.[0]
x = x || {};                      →  x ??= {};
```

`||` treating `0`, `""` and `false` as absent is a real bug class, not a style
point. `??` is the one you almost always meant.

## Declarations

```ts
var x = 1;                        →  const x = 1;                     // or let, if reassigned
let total = 0; /* never reassigned */ → const total = 0;
let cfg: Config; cfg = load();    →  const cfg = load();
```

## Strings

```ts
"Hello, " + name + "!"            →  `Hello, ${name}!`
["a","b"].join(", ")              →  (already right — leave it)
str.substr(0, 5)                  →  str.slice(0, 5)                  // substr is deprecated
str.indexOf("x") !== -1           →  str.includes("x")
str.charAt(0) === "a"             →  str.startsWith("a")
```

## Arrays

```ts
for (let i = 0; i < xs.length; i++) { const x = xs[i]; … }
                                  →  for (const x of xs) { … }
for (const i in xs)               →  for (const x of xs)              // for..in gives string keys
xs.indexOf(y) !== -1              →  xs.includes(y)                   // and handles NaN
xs.filter(f).length > 0           →  xs.some(f)
xs.filter(f)[0]                   →  xs.find(f)
xs.filter(f).length === 0         →  !xs.some(f)
const out = []; xs.forEach(x => out.push(g(x)));
                                  →  const out = xs.map(g);
xs.sort(f)                        →  [...xs].sort(f)  /  xs.toSorted(f)   // sort mutates
xs.reverse()                      →  xs.toReversed()                  // so does reverse
Array.prototype.slice.call(a)     →  Array.from(a)  /  [...a]
new Array(n).fill(0)              →  Array.from({ length: n }, () => 0)
f.apply(null, args)               →  f(...args)
```

## Objects

```ts
obj.hasOwnProperty(k)             →  Object.hasOwn(obj, k)
Object.keys(o).forEach(k => use(o[k]))
                                  →  for (const [k, v] of Object.entries(o)) use(v)
Object.assign({}, a, b)           →  { ...a, ...b }
Object.assign(target, patch)      →  { ...target, ...patch }          // stop mutating target
JSON.parse(JSON.stringify(x))     →  structuredClone(x)               // JSON drops Date/Map/undefined
const { a } = obj; const { b } = obj; → const { a, b } = obj;
```

## Types

```ts
<Foo>value                        →  value as Foo                     // <> is illegal in .tsx
value as any                      →  narrow it, or `unknown` + a guard
const x = { a: 1 } as Config      →  const x = { a: 1 } satisfies Config   // keeps literal types
function f(x: Function)           →  function f(x: (n: number) => void)
type T = {}                       →  type T = Record<string, unknown>  /  object
enum Status { A = "a" }           →  type Status = "a" | "b"           // or an `as const` object
namespace Utils { … }             →  a module with named exports
Array<string>                     →  string[]                          // cosmetic; be consistent
interface Props { children?: ReactNode } with React.FC
                                  →  function C(props: Props)          // FC's implicit children is gone
```

## Modules

```ts
const x = require("y");           →  import x from "y";
module.exports = f;               →  export default f;   // or a named export, usually better
import { User } from "./types";   →  import type { User } from "./types";   // when only a type
export * from "./a";              →  export { thing } from "./a";       // name what you re-export
import "../../../shared/util";    →  import "@shared/util";             // tsconfig paths
```

## Dates and numbers

```ts
new Date().getTime()              →  Date.now()
parseInt(s)                       →  parseInt(s, 10)  /  Number(s)
isNaN(x)                          →  Number.isNaN(x)                   // global isNaN coerces
x.toFixed(2) as unknown as number →  Number(x.toFixed(2))
```

## Errors

```ts
throw "bad input";                →  throw new Error("bad input");
catch (e) { throw new Error(e.message) }
                                  →  catch (e) { throw new Error("…", { cause: e }) }
catch (e: any) { … }              →  catch (e) { if (e instanceof Error) … }   // e is unknown
Promise.reject("nope")            →  Promise.reject(new Error("nope"))
```

## Promises

```ts
doThing();                        →  await doThing();  /  void doThing().catch(report);
p.then(a).then(b).catch(c)        →  try { const x = await p; … } catch (c) { … }
new Promise(async (res) => …)     →  the async function's own promise
xs.forEach(async x => await f(x)) →  await Promise.all(xs.map(f))   // or for..of for sequential
for (const x of xs) await f(x)    →  await Promise.all(xs.map(f))   // only when independent
```

## Classes

```ts
class A { private x: number; constructor(x: number) { this.x = x; } }
                                  →  class A { constructor(private readonly x: number) {} }
class Util { static f() {} }      →  export function f() {}
get x() { return this._x } set x(v) { this._x = v }
                                  →  a public field
constructor() { super(); }        →  (delete it)
```

## What not to change

- `for` loops that genuinely need the index, an early exit with side effects, or
  `await` per iteration.
- `any` in a `.d.ts` describing a third-party API you do not control.
- A cast at a boundary with a runtime check beside it.
- `interface` vs `type`, `T[]` vs `Array<T>`, arrow vs `function` — pick one per
  codebase and stop. Consistency is the value; the choice is not.
