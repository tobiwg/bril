import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

# Load CSV
file_path = "./lvn-results.csv"
df = pd.read_csv(file_path)

# Keep track of incorrect/missing/timeout
incorrect_df = df[df["result"].isin(["incorrect", "missing", "timeout"])]
incorrect_counts = incorrect_df["result"].value_counts().to_dict()

# Clean data for numeric analysis
df_clean = df[pd.to_numeric(df["result"], errors="coerce").notna()]
df_clean["result"] = df_clean["result"].astype(int)

# Pivot
pivot_df = df_clean.pivot(index="benchmark", columns="run", values="result").reset_index()

# Compute improvements
pivot_df["improvement"] = pivot_df["baseline"] - pivot_df["LVN"]
pivot_df["percent_improvement"] = 100 * pivot_df["improvement"] / pivot_df["baseline"]

# Compute stats
total_benchmarks = len(pivot_df)
improved = (pivot_df["improvement"] > 0).sum()
unchanged = (pivot_df["improvement"] == 0).sum()
regressed = (pivot_df["improvement"] < 0).sum()
total_improvement = pivot_df["improvement"].sum()
avg_improvement = pivot_df["improvement"].mean()
mean_percent_improvement = pivot_df["percent_improvement"].mean()

# Summary with incorrect/missing/timeout
summary = {
    "Total Benchmarks": total_benchmarks,
    "Improved": improved,
    "Unchanged": unchanged,
    "Regressed": regressed,
    "Incorrect Results (LVN)": incorrect_counts.get("incorrect", 0),
    "Missing Results (LVN)": incorrect_counts.get("missing", 0),
    "Timeout Results (LVN)": incorrect_counts.get("timeout", 0),
    "Total Absolute Improvement": total_improvement,
    "Average Improvement": round(avg_improvement, 2),
    "Mean Percent Improvement": round(mean_percent_improvement, 2),
}

# Save analysis output
analysis_path = "./LVN_performance_analysis.csv"
pivot_df.to_csv(analysis_path, index=False)

# Plot improvements
plt.figure(figsize=(14, 7))
sorted_df = pivot_df.sort_values("improvement", ascending=False)
sns.barplot(x="benchmark", y="improvement", data=sorted_df)
plt.xticks(rotation=90)
plt.title("Improvement of LVN over baseline")
plt.xlabel("Benchmark")
plt.ylabel("Improvement")
plt.tight_layout()

plot_path = "./LVN_improvements_plot.png"
plt.savefig(plot_path)
print("Analysis complete. Summary:", summary)

summary, analysis_path, plot_path
