#include "llvm/IR/PassManager.h"
#include "llvm/IR/Instructions.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"      // ← REQUIRED for PassPluginLibraryInfo
#include "llvm/Transforms/Utils/Local.h"
#include "llvm/Support/raw_ostream.h"

using namespace llvm;

namespace {

struct SimpleDCEPass : PassInfoMixin<SimpleDCEPass> {
  PreservedAnalyses run(Function &F, FunctionAnalysisManager &) {
    bool Changed = false;
    SmallVector<Instruction*> ToDelete;

    for (auto &BB : F)
      for (auto &I : BB)
        if (isInstructionTriviallyDead(&I))
          ToDelete.push_back(&I);

    for (Instruction *I : ToDelete)
      if (RecursivelyDeleteTriviallyDeadInstructions(I)){
        Changed = true;
        errs() << "changed";
      }

    return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
  }
};

} // namespace

// ---- Plugin boilerplate ----
extern "C" LLVM_ATTRIBUTE_WEAK PassPluginLibraryInfo llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "SimpleDCE", LLVM_VERSION_STRING,
          [](PassBuilder &PB) {
            PB.registerPipelineParsingCallback(
                [](StringRef Name, FunctionPassManager &FPM,
                   ArrayRef<PassBuilder::PipelineElement>) {
                  if (Name == "simple-dce") {
                    FPM.addPass(SimpleDCEPass());
                    return true;
                  }
                  return false;
                });
          }};
}
