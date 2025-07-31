import type { Investment } from "@/types/investment";

const STORAGE_KEY = 'user_investments';

export const investmentStorage = {
  getInvestments(): Investment[] {
    try {
      const data = localStorage.getItem(STORAGE_KEY);
      return data ? JSON.parse(data) : [];
    } catch (error) {
      console.error('Failed to load investments:', error);
      return [];
    }
  },

  saveInvestment(investment: Investment): void {
    try {
      const investments = this.getInvestments();
      investments.push(investment);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(investments));
    } catch (error) {
      console.error('Failed to save investment:', error);
    }
  },

  updateInvestment(investmentId: string, updates: Partial<Investment>): void {
    try {
      const investments = this.getInvestments();
      const index = investments.findIndex(inv => inv.id === investmentId);
      if (index !== -1) {
        investments[index] = { ...investments[index], ...updates };
        localStorage.setItem(STORAGE_KEY, JSON.stringify(investments));
      }
    } catch (error) {
      console.error('Failed to update investment:', error);
    }
  },

  exportInvestments(): string {
    const investments = this.getInvestments();
    return JSON.stringify(investments, null, 2);
  },

  importInvestments(jsonData: string): boolean {
    try {
      const investments = JSON.parse(jsonData);
      if (Array.isArray(investments)) {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(investments));
        return true;
      }
      return false;
    } catch (error) {
      console.error('Failed to import investments:', error);
      return false;
    }
  }
};