import React from "react";
import { Scissors, ShieldAlert, ArrowRight, Eye, PlayCircle, CheckCircle2 } from "lucide-react";
import { Opportunity, OpportunityType, OperationalRiskLevel, DecisionRecord, DecisionStatus } from "../types";

interface RecommendationPanelProps {
  opportunities: Opportunity[];
  decisions: Map<string, DecisionRecord>;
  onInspectEvidence: (opp: Opportunity) => void;
  onOpenSimulator: (opp: Opportunity) => void;
}

interface RankedRecommendation {
  opportunity: Opportunity;
  rank: number;
  targetVendor: string;
  recommendedAction: string;
  potentialSavingsMonthly: number;
  isUnknownCharge: boolean;
  whyPoints: string[];
  riskLevel: OperationalRiskLevel;
  isAlreadyDecided: boolean;
  decisionStatus?: DecisionStatus;
}

export const RecommendationPanel: React.FC<RecommendationPanelProps> = ({
  opportunities,
  decisions,
  onInspectEvidence,
  onOpenSimulator,
}) => {
  // Rank and extract fact-based recommendations
  const recommendations: RankedRecommendation[] = React.useMemo(() => {
    if (!opportunities || opportunities.length === 0) return [];

    // Sort opportunities by score descending (Python backend already provides score, but we ensure stable sort)
    const sorted = [...opportunities].sort((a, b) => {
      const scoreA = a.ranking_score ?? a.score ?? 0;
      const scoreB = b.ranking_score ?? b.score ?? 0;
      return scoreB - scoreA;
    });

    return sorted.map((opp, idx) => {
      const decision = decisions.get(opp.id);
      const isAlreadyDecided = !!decision && decision.decision_status !== DecisionStatus.UNREVIEWED;

      const isUnknown =
        opp.type === OpportunityType.UNKNOWN_RECURRING ||
        opp.type === OpportunityType.UNKNOWN_RECURRING_CHARGE ||
        opp.type.includes("UNKNOWN");

      const isOverlap =
        opp.type === OpportunityType.POTENTIAL_OVERLAP ||
        opp.type === OpportunityType.OVERLAPPING_TOOLS ||
        opp.type.includes("OVERLAP");

      const isPriceIncrease =
        opp.type === OpportunityType.PRICE_INCREASE ||
        opp.type.includes("PRICE");

      // Target tool name
      let targetVendor = opp.primary_vendor || (opp.vendors && opp.vendors[0]) || opp.title;
      if (isOverlap && opp.vendors && opp.vendors.length >= 2) {
        // If overlap, highlight the second tool or combined pair
        targetVendor = `${opp.vendors[1] || opp.vendors[0]} (vs. ${opp.vendors[0]})`;
      }

      // Recommended action
      let recommendedAction = "Review first";
      if (isPriceIncrease) {
        recommendedAction = "Downgrade or negotiate rate";
      } else if (isOverlap) {
        recommendedAction = "Consolidate or cancel redundant tool";
      } else if (isUnknown) {
        recommendedAction = "Investigate unverified charge";
      } else {
        recommendedAction = "Review subscription necessity";
      }

      // Build strictly fact-based WHY points from real evidence
      const whyPoints: string[] = [];

      if (isOverlap) {
        whyPoints.push(`${opp.category || "Tool"} overlap detected with ${opp.vendors?.join(" & ") || "counterpart tool"}`);
        if (opp.evidence?.facts && opp.evidence.facts.length > 0) {
          opp.evidence.facts.forEach((f) => {
            if (f.toLowerCase().includes("monthly") || f.toLowerCase().includes("cadence") || f.toLowerCase().includes("transaction")) {
              whyPoints.push(f);
            }
          });
        } else {
          whyPoints.push("Multiple active tools observed in identical software capability category");
        }
      } else if (isPriceIncrease) {
        if (opp.evidence?.facts && opp.evidence.facts.length > 0) {
          whyPoints.push(...opp.evidence.facts.slice(0, 3));
        } else {
          whyPoints.push(`Observed billing amount increased across consecutive statement cycles`);
        }
      } else if (isUnknown) {
        whyPoints.push("Merchant name not recognized in verified SaaS vendor directory");
        whyPoints.push("Requires internal verification before confirming any savings");
      } else {
        if (opp.evidence?.facts && opp.evidence.facts.length > 0) {
          whyPoints.push(...opp.evidence.facts.slice(0, 3));
        } else {
          whyPoints.push(`High observed recurring commitment of $${opp.monthly_amount.toFixed(2)}/month`);
        }
      }

      // Determine risk level (fallback to LOW/MEDIUM)
      const risk =
        opp.operational_risk ||
        opp.risk_level ||
        (isUnknown ? OperationalRiskLevel.HIGH : isOverlap ? OperationalRiskLevel.MODERATE : OperationalRiskLevel.LOW);

      return {
        opportunity: opp,
        rank: idx + 1,
        targetVendor,
        recommendedAction,
        potentialSavingsMonthly: opp.monthly_amount || 0,
        isUnknownCharge: isUnknown,
        whyPoints: Array.from(new Set(whyPoints)).slice(0, 4),
        riskLevel: risk,
        isAlreadyDecided,
        decisionStatus: decision?.decision_status,
      };
    });
  }, [opportunities, decisions]);

  if (!opportunities || opportunities.length === 0) {
    return null;
  }

  const getRiskBadge = (risk: OperationalRiskLevel | string) => {
    switch (risk) {
      case OperationalRiskLevel.HIGH:
      case OperationalRiskLevel.CRITICAL:
      case "HIGH":
      case "CRITICAL":
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-rose-500/10 border border-rose-500/20 text-rose-300 uppercase">
            Risk: High
          </span>
        );
      case OperationalRiskLevel.MODERATE:
      case "MEDIUM":
      case "MODERATE":
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/10 border border-amber-500/20 text-amber-300 uppercase">
            Risk: Medium
          </span>
        );
      default:
        return (
          <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-emerald-500/10 border border-emerald-500/20 text-emerald-300 uppercase">
            Risk: Low
          </span>
        );
    }
  };

  return (
    <div id="what-should-i-cut-panel" className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-rose-500/10 border border-rose-500/20 flex items-center justify-center text-rose-400">
            <Scissors className="w-4 h-4" />
          </div>
          <div>
            <h2 className="text-sm font-bold text-slate-100 uppercase tracking-wide">
              What Should I Cut?
            </h2>
            <p className="text-[11px] text-slate-400">
              Prioritized recommendations based on observed recurrence evidence and category overlap
            </p>
          </div>
        </div>
        <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-400 bg-slate-800/80 px-2.5 py-1 rounded">
          {recommendations.length} actionable {recommendations.length === 1 ? "target" : "targets"}
        </span>
      </div>

      {/* Ranked Recommendation Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {recommendations.slice(0, 3).map((rec) => {
          const { opportunity: opp } = rec;

          return (
            <div
              key={opp.id}
              id={`recommendation-card-${opp.id}`}
              className="bg-slate-950/70 border border-slate-800 hover:border-slate-700 rounded-xl p-5 flex flex-col justify-between transition-colors relative space-y-4"
            >
              {/* Card Header & Rank */}
              <div>
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-black px-2 py-0.5 rounded bg-indigo-500/20 text-indigo-300 border border-indigo-500/30">
                      #{rec.rank}
                    </span>
                    <h3 className="text-sm font-bold text-slate-100 line-clamp-1">
                      {rec.targetVendor}
                    </h3>
                  </div>
                  {getRiskBadge(rec.riskLevel)}
                </div>

                {/* Recommended Action & Potential Savings */}
                <div className="mt-3 p-3 bg-slate-900/90 border border-slate-800/80 rounded-lg space-y-1">
                  <div className="text-[10px] uppercase font-bold text-slate-400">
                    Recommended Action
                  </div>
                  <div className="text-xs font-semibold text-indigo-300">
                    {rec.recommendedAction}
                  </div>

                  <div className="pt-2 mt-2 border-t border-slate-800 flex items-center justify-between">
                    <span className="text-[10px] uppercase font-bold text-slate-400">
                      Potential Savings
                    </span>
                    <span className="text-xs font-bold text-emerald-400">
                      {rec.isUnknownCharge
                        ? "$0/mo (Under Investigation)"
                        : `$${rec.potentialSavingsMonthly.toFixed(2)}/mo`}
                    </span>
                  </div>
                </div>

                {/* WHY section */}
                <div className="mt-3 space-y-1.5">
                  <div className="text-[10px] uppercase font-black tracking-wider text-slate-400">
                    Why?
                  </div>
                  <ul className="space-y-1 pl-1">
                    {rec.whyPoints.map((point, pIdx) => (
                      <li
                        key={pIdx}
                        className="text-[11px] text-slate-300 flex items-start gap-1.5 leading-relaxed"
                      >
                        <span className="text-indigo-400 text-xs font-bold">•</span>
                        <span>{point}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              </div>

              {/* Action Buttons */}
              <div className="pt-3 border-t border-slate-800/80 flex items-center gap-2">
                <button
                  id={`btn-review-evidence-${opp.id}`}
                  onClick={() => onInspectEvidence(opp)}
                  className="flex-1 px-3 py-2 bg-slate-800 hover:bg-slate-700 text-slate-200 text-xs font-medium rounded-lg transition-colors flex items-center justify-center gap-1.5 cursor-pointer"
                >
                  <Eye className="w-3.5 h-3.5" />
                  <span>Review Evidence</span>
                </button>

                <button
                  id={`btn-stage-decision-${opp.id}`}
                  onClick={() => onOpenSimulator(opp)}
                  className="flex-1 px-3 py-2 bg-indigo-600 hover:bg-indigo-500 text-white text-xs font-semibold rounded-lg transition-colors flex items-center justify-center gap-1.5 cursor-pointer shadow-sm"
                >
                  <PlayCircle className="w-3.5 h-3.5" />
                  <span>Stage Decision</span>
                </button>
              </div>

              {/* Status indicator if already staged or decided */}
              {rec.isAlreadyDecided && (
                <div className="absolute top-2 right-2 -mt-1 -mr-1">
                  <span className="flex h-3 w-3 relative">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                    <span className="relative inline-flex rounded-full h-3 w-3 bg-emerald-500"></span>
                  </span>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};
