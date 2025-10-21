Team: Adnan Al Armouti, Tobias Weinberg

##  Dead Code Elimination on LLVM

For this homework, we implemented a simple Dead Code Elimination (DCE) pass using the LLVM pass manager. The goal was to delete instructions that don’t affect the program’s output — basically anything that computes a value that’s never used and has no side effects.

Our pass runs at the function level and uses LLVM’s built-in helpers `isInstructionTriviallyDead()` and `RecursivelyDeleteTriviallyDeadInstructions()` to keep things safe and simple.

```cpp
struct SimpleDCEPass : PassInfoMixin<SimpleDCEPass> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
    bool Changed = false;
    SmallVector<Instruction*> ToDelete;

    for (auto &BB : F)
      for (auto &I : BB)
        if (isInstructionTriviallyDead(&I))
          ToDelete.push_back(&I);

    for (Instruction *I : ToDelete)
      if (RecursivelyDeleteTriviallyDeadInstructions(I))
        Changed = true;

    return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
  }
};
```


### What didn’t work (at first)

We ran into a lot of build and toolchain issues before seeing anything happen:

1. Apple Clang vs Homebrew LLVM
macOS ships its own clang that’s built differently and doesn’t support -fpass-plugin or -mllvm -passes=.... Mixing the two caused crashes.
Fix: build and run everything using Homebrew LLVM (/opt/homebrew/opt/llvm/bin/clang).

2. optnone stopped all optimization
At -O0, clang adds an optnone attribute that literally blocks every pass. Our DCE didn’t run at all until we re-emitted the IR with 
``` 
-Xclang -disable-O0-optnone
```
Stack allocas and dead stores
At -O0, C locals become stack variables (alloca + store). Stores have side effects, so LLVM considers them non-trivial. That’s why lines like

```
int b = 5; // "dead" 
```
didn’t get removed.
→ Fix: run mem2reg before our pass, so variables become SSA values. Then DCE can remove unused ones.
→ Bonus: adding dse (Dead Store Elimination) gets rid of actual dead stores.

4. No visible difference
Even when everything worked, diff between test.ll and optimized.ll often showed nothing because optnone or stores blocked the effect. Once we chained mem2reg → simple-dce → dse, the dead code finally vanished.
Final working command
```
clang -O0 -S -emit-llvm \
  -Xclang -disable-O0-optnone \
  -fpass-plugin=./build/skeleton/SkeletonPass.dylib \
  -mllvm -passes='mem2reg,function(simple-dce),dse' \
  test.c -o optimized.ll
  ```
### Takeaways

- Always check you’re using the same LLVM toolchain for building and running your pass.

- Disable optnone or your pass will never run.

- Stack variables need mem2reg before DCE can touch them.

- For dead stores, pair DCE with dse.

- -debug-pass-manager is your friend for checking if your pass actually runs.

Overall, once we ironed out the build and “why is nothing deleting?!” issues, the pass itself was surprisingly short and satisfying.

#### GenAI disclaimer

We used ChatGPT (GPT-5) to help debug toolchain problems and write shell commands for running the pass. It was great at explaining LLVM’s new pass manager and helping with CMake flags, but it often hallucinated compiler options or mixed up Apple Clang and Homebrew LLVM behavior — which caused a few confusing detours before we realized the issue was optnone all along. Once we fact-checked the suggestions and verified commands manually, it was a super useful debugging assistant — just not a substitute for actually reading LLVM’s docs.