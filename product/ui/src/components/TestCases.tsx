import { useEffect, useState } from 'react'
import type { suiteChecks } from '../suite'

export function TestCases({ group }: { group: ReturnType<typeof suiteChecks>[number] }) {
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [page, setPage] = useState(0)
  useEffect(() => { setQuery(''); setFilter('all'); setPage(0) }, [group.id])
  const matching = group.cases.filter(test => test.name.toLowerCase().includes(query.toLowerCase()) && (filter === 'all' || filter === 'unpassed' ? filter === 'all' || test.state !== 'passed' : test.state === filter))
  const lastPage = Math.max(0, Math.ceil(matching.length / 20) - 1)
  const currentPage = Math.min(page,lastPage)
  return <div className="test-case-list"><p><b>{group.passed} / {group.total} passed</b> · {group.failed} failed</p><progress aria-label="Tests passed" value={group.passed} max={Math.max(group.total,1)} />
    <label>Find a test<input value={query} onChange={event => { setQuery(event.target.value); setPage(0) }} placeholder="Search test names" /></label>
    <label>Show<select value={filter} onChange={event => { setFilter(event.target.value); setPage(0) }}><option value="all">All tests</option><option value="unpassed">Not passed</option><option value="failed">Failed</option><option value="passed">Passed</option></select></label>
    {matching.slice(currentPage * 20, currentPage * 20 + 20).map(test => <details key={test.id}><summary>{test.name}<small>{test.state}</small></summary><p>{test.description}</p>{test.event?.testResult ? <><b>Expected</b><p>{test.event.testResult.expected}</p><b>Observed</b><p>{test.event.testResult.observed}</p></> : <p>No result yet.</p>}</details>)}
    {!matching.length && <p>No tests match this filter.</p>}
    {matching.length > 20 && <div className="test-pagination"><button disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous</button><span>{currentPage + 1} / {lastPage + 1}</span><button disabled={currentPage === lastPage} onClick={() => setPage(currentPage + 1)}>Next</button></div>}
  </div>
}
