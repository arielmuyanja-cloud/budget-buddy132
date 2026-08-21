import React, { useMemo } from "react";
import { TrendingUp, TrendingDown, Calendar, AlertCircle, Sparkles } from "lucide-react";
import { Transaction, RecurringCharge, Opportunity, OpportunityType } from "../types";

interface SoftwareWasteTimelineProps {
  transactions: Transaction[];
  recurringCharges: RecurringCharge[];
  opportunities: Opportunity[];
}

interface MonthlySpend {
  monthKey: string; // e.g. "2026-01"
  displayLabel: string; // e.g. "Jan 2026"
  shortLabel: string; // e.g. "Jan"
  totalSpend: number;
  transactionCount: number;
}

export const SoftwareWasteTimeline: React.FC<SoftwareWasteTimelineProps> = ({
  transactions,
  recurringCharges,
  opportunities,
}) => {
  // Aggregate actual software transactions by calendar month
  const monthlyData = useMemo<MonthlySpend[]>(() => {
    if (!transactions || transactions.length === 0) return [];

    // Create a set of software vendor names & fingerprints from recurring charges
    const softwareMerchants = new Set<string>();
    recurringCharges.forEach((rc) => {
      if (rc.is_software) {
        if (rc.clean_merchant) softwareMerchants.add(rc.clean_merchant.toLowerCase());
        if (rc.raw_merchant) softwareMerchants.add(rc.raw_merchant.toLowerCase());
      }
    });

    const monthMap = new Map<string, { total: number; count: number }>();

    transactions.forEach((tx) => {
      if (!tx.date) return;
      // Do not treat refunds/negative transactions as positive software spend
      if (typeof tx.amount !== "number" || tx.amount <= 0) return;
      
      // Check if transaction is software-related
      const cleanM = (tx.clean_merchant || tx.description || "").toLowerCase();
      const rawM = (tx.raw_merchant || tx.raw_description || "").toLowerCase();
      const cat = (tx.category || "").toLowerCase();
      
      const isSoftware =
        softwareMerchants.has(cleanM) ||
        softwareMerchants.has(rawM) ||
        cat.includes("software") ||
        cat.includes("saas") ||
        cat.includes("subscription") ||
        cat.includes("cloud") ||
        recurringCharges.some(
          (rc) => rc.is_software && (rc.fingerprint === tx.fingerprint || cleanM.includes(rc.clean_merchant?.toLowerCase() || ""))
        );

      if (!isSoftware) return;

      const dateObj = new Date(tx.date);
      if (isNaN(dateObj.getTime())) return;

      const year = dateObj.getUTCFullYear();
      const month = String(dateObj.getUTCMonth() + 1).padStart(2, "0");
      const monthKey = `${year}-${month}`;

      const current = monthMap.get(monthKey) || { total: 0, count: 0 };
      monthMap.set(monthKey, {
        total: current.total + tx.amount,
        count: current.count + 1,
      });
    });

    // Sort chronologically
    const sortedKeys = Array.from(monthMap.keys()).sort();

    const monthNames = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

    return sortedKeys.map((k) => {
      const [y, m] = k.split("-");
      const mIndex = parseInt(m, 10) - 1;
      const shortLabel = monthNames[mIndex] || m;
      const displayLabel = `${shortLabel} ${y}`;
      const entry = monthMap.get(k)!;

      return {
        monthKey: k,
        displayLabel,
        shortLabel,
        totalSpend: Math.round(entry.total * 100) / 100,
        transactionCount: entry.count,
      };
    });
  }, [transactions, recurringCharges]);

  // Identify supported causes of changes from actual data
  const changeDrivers = useMemo<string[]>(() => {
    const drivers: string[] = [];

    // 1. Price increases detected
    recurringCharges.forEach((rc) => {
      if (rc.has_price_increase && rc.price_increase_delta) {
        const delta = Math.round(rc.price_increase_delta * 100) / 100;
        drivers.push(`${rc.clean_merchant} price increase: +$${delta.toFixed(2)}/mo`);
      }
    });

    // 2. Newly started subscriptions (first seen in later month than starting month)
    if (monthlyData.length >= 2) {
      const startMonthKey = monthlyData[0].monthKey;
      recurringCharges.forEach((rc) => {
        // Find earliest transaction date for this recurring charge
        const matchingTxs = transactions.filter(
          (t) =>
            t.clean_merchant?.toLowerCase() === rc.clean_merchant.toLowerCase() ||
            t.raw_merchant?.toLowerCase() === rc.raw_merchant.toLowerCase() ||
            t.fingerprint === rc.fingerprint
        );
        if (matchingTxs.length > 0) {
          const earliestDate = matchingTxs.reduce((min, t) => (t.date < min ? t.date : min), matchingTxs[0].date);
          if (earliestDate) {
            const firstTxMonth = earliestDate.slice(0, 7);
            if (firstTxMonth > startMonthKey && rc.is_known_vendor) {
              drivers.push(
                `New subscription: ${rc.clean_merchant} +$${rc.monthly_equivalent.toFixed(2)}/mo`
              );
            }
          }
        }
      });
    }

    // 3. Unknown recurring charges
    recurringCharges.forEach((rc) => {
      if (!rc.is_known_vendor) {
        drivers.push(
          `Unknown recurring charge: ${rc.clean_merchant} +$${rc.monthly_equivalent.toFixed(2)}/mo`
        );
      }
    });

    // 4. Overlaps detected
    opportunities.forEach((opp) => {
      if (
        opp.type === OpportunityType.POTENTIAL_OVERLAP ||
        opp.type === OpportunityType.OVERLAPPING_TOOLS ||
        opp.type.includes("OVERLAP")
      ) {
        const vendorNames = opp.vendors && opp.vendors.length > 0 ? opp.vendors.join(" & ") : opp.title;
        drivers.push(`${opp.category || "Tool"} overlap detected (${vendorNames})`);
      }
    });

    // Deduplicate drivers
    return Array.from(new Set(drivers));
  }, [recurringCharges, opportunities, transactions, monthlyData]);

  // If not enough months
  if (monthlyData.length < 2) {
    return (
      <div id="software-waste-timeline" className="bg-slate-900 border border-slate-800 rounded-xl p-6">
        <div className="flex items-center gap-2 mb-4">
          <div className="w-8 h-8 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <Calendar className="w-4 h-4" />
          </div>
          <div>
            <h2 className="text-sm font-bold text-slate-100 tracking-wide">SOFTWARE SPEND OVER TIME</h2>
            <p className="text-[11px] text-slate-400">Monthly spend aggregation from statement dates</p>
          </div>
        </div>

        <div className="py-8 text-center text-slate-400 text-xs flex flex-col items-center justify-center gap-2 border border-dashed border-slate-800 rounded-lg">
          <AlertCircle className="w-5 h-5 text-slate-500" />
          <span>Not enough historical data to show a spending trend.</span>
          <span className="text-[11px] text-slate-500">
            (Statement contains {monthlyData.length === 1 ? `1 month (${monthlyData[0].displayLabel})` : "no software transactions"})
          </span>
        </div>
      </div>
    );
  }

  // Calculate change over period
  const firstMonth = monthlyData[0];
  const lastMonth = monthlyData[monthlyData.length - 1];
  const diff = lastMonth.totalSpend - firstMonth.totalSpend;
  const isIncrease = diff > 0;
  const isDecrease = diff < 0;
  const maxSpend = Math.max(...monthlyData.map((m) => m.totalSpend), 1);

  return (
    <div id="software-waste-timeline" className="bg-slate-900 border border-slate-800 rounded-xl p-6 space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-800/80 pb-4">
        <div className="flex items-center gap-3">
          <div className="w-9 h-9 rounded-lg bg-indigo-500/10 border border-indigo-500/20 flex items-center justify-center text-indigo-400">
            <Calendar className="w-4 h-4" />
          </div>
          <div>
            <h2 className="text-sm font-bold text-slate-100 uppercase tracking-wide">
              Software Spend Over Time
            </h2>
            <p className="text-[11px] text-slate-400">
              Aggregated across {monthlyData.length} consecutive statement periods
            </p>
          </div>
        </div>

        {/* Change Metric Badge */}
        <div
          id="timeline-spend-delta-badge"
          className={`inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs font-semibold ${
            isIncrease
              ? "bg-rose-500/10 border-rose-500/20 text-rose-300"
              : isDecrease
              ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-300"
              : "bg-slate-800 border-slate-700 text-slate-300"
          }`}
        >
          {isIncrease ? (
            <TrendingUp className="w-4 h-4 text-rose-400" />
          ) : isDecrease ? (
            <TrendingDown className="w-4 h-4 text-emerald-400" />
          ) : null}
          <span>
            {isIncrease ? "+" : isDecrease ? "-" : ""}
            ${Math.abs(diff).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}/month since {firstMonth.shortLabel}
          </span>
        </div>
      </div>

      {/* Monthly Timeline Cards & Bar Visualization */}
      <div className="space-y-4">
        <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-6 gap-3">
          {monthlyData.map((m) => {
            const heightPercent = Math.max(Math.round((m.totalSpend / maxSpend) * 100), 12);
            return (
              <div
                key={m.monthKey}
                id={`timeline-month-${m.monthKey}`}
                className="bg-slate-950/60 border border-slate-800 rounded-lg p-3 flex flex-col justify-between hover:border-slate-700 transition-colors"
              >
                <div>
                  <div className="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
                    {m.shortLabel}
                  </div>
                  <div className="text-sm sm:text-base font-bold text-slate-100 mt-0.5">
                    ${m.totalSpend.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}
                  </div>
                  <div className="text-[10px] text-slate-500 mt-0.5">
                    {m.transactionCount} charges
                  </div>
                </div>

                {/* Relative Bar indicator */}
                <div className="w-full bg-slate-800/80 rounded-full h-1.5 mt-3 overflow-hidden">
                  <div
                    className="bg-indigo-500 h-full rounded-full transition-all duration-300"
                    style={{ width: `${heightPercent}%` }}
                  />
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Drivers / What's driving the change */}
      <div className="border-t border-slate-800/80 pt-4 space-y-2.5">
        <div className="flex items-center gap-2">
          <Sparkles className="w-3.5 h-3.5 text-amber-400" />
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-300">
            What's Driving the Change?
          </h3>
        </div>

        {changeDrivers.length > 0 ? (
          <ul className="space-y-2 pl-1">
            {changeDrivers.map((driver, idx) => (
              <li
                key={idx}
                className="text-xs text-slate-300 flex items-start gap-2 bg-slate-950/40 p-2 rounded-md border border-slate-800/60"
              >
                <span className="text-indigo-400 font-bold">•</span>
                <span>{driver}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-slate-500 italic pl-1">
            No unexpected price spikes or recurring software anomalies detected across historical periods.
          </p>
        )}
      </div>
    </div>
  );
};
