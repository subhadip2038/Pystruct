import numpy as np

from sklearn.externals.joblib import Parallel, delayed, cpu_count
from sklearn.utils import gen_even_slices

from .ssvm import BaseSSVM
from ..utils import find_constraint


class SubgradientSSVM(BaseSSVM):
    """Structured SVM solver using subgradient descent.

    Implements a margin rescaled with l1 slack penalty.
    By default, a constant learning rate is used.
    It is also possible to use the adaptive learning rate found by AdaGrad.

    This class implements online subgradient descent. If n_jobs != 1,
    small batches of size n_jobs are used to exploit parallel inference.
    If inference is fast, use n_jobs=1.

    Parmeters
    ---------
    problem : StructuredProblem
        Object containing problem formulation. Has to implement
        `loss`, `inference` and `loss_augmented_inference`.

    max_iter : int, default=100
        Maximum number of passes over dataset to find constraints and perform
        updates.

    C : float, default=1.
        Regularization parameter

    verbose : int, default=0
        Verbosity.

    learning_rate : float, default=0.001
        Learning rate used in subgradient descent.

    momentum : float, default=0.9
        Momentum used in subgradient descent.

    adagrad : bool (default=False)
        Whether to use adagrad gradient scaling.
        Ignores if True, momentum is ignored.

    n_jobs : int, default=1
        Number of parallel jobs for inference. -1 means as many as cpus.

    batch_size : int, default=None
        Ignored if n_jobs > 1. If n_jobs=1, inference will be done in batches
        of size batch_size.

    show_loss_every : int, default=0
        Controlls how often the hamming loss is computed (for monitoring
        purposes). Zero means never, otherwise it will be computed very
        show_loss_every'th epoch.

    decay_exponent : float, default=0
        Exponent for decaying learning rate. Effective learning rate is
        ``learning_rate / t ** decay_exponent``. Zero means no decay.
         Ignored if adagrad=True.

    break_on_no_constraints : bool, default=True
        Break when there are no new constraints found.

    Attributes
    ----------
    w : nd-array, shape=(problem.psi,)
        The learned weights of the SVM.

   ``loss_curve_`` : list of float
        List of loss values if show_loss_every > 0.

   ``objective_curve_`` : list of float
       Primal objective after each pass through the dataset.

    """
    def __init__(self, problem, max_iter=100, C=1.0, verbose=0, momentum=0.9,
                 learning_rate=0.001, adagrad=False, n_jobs=1,
                 show_loss_every=0, decay_exponent=0,
                 break_on_no_constraints=True, logger=None, batch_size=None):
        BaseSSVM.__init__(self, problem, max_iter, C, verbose=verbose,
                          n_jobs=n_jobs, show_loss_every=show_loss_every,
                          logger=logger)
        self.break_on_no_constraints = break_on_no_constraints
        self.momentum = momentum
        self.learning_rate = learning_rate
        self.t = 0
        self.adagrad = adagrad
        self.grad_old = np.zeros(self.problem.size_psi)
        self.decay_exponent = decay_exponent
        self.batch_size = batch_size

    def _solve_subgradient(self, dpsi, n_samples):
        """Do a single subgradient step."""

        #w += 1. / self.t * (psi_matrix - w / self.C / 2)
        #grad = (self.learning_rate / (self.t + 1.) ** 2
                #* (psi_matrix - w / self.C / 2))
        grad = (dpsi - self.w / (self.C * n_samples))

        if self.adagrad:
            self.grad_old += grad ** 2
            self.w += self.learning_rate * grad / (1. + np.sqrt(self.grad_old))
            print("grad old %f" % np.mean(self.grad_old))
            print("effective lr %f" % (self.learning_rate /
                                       np.mean(1. + np.sqrt(self.grad_old))))
        else:
            self.grad_old = ((1 - self.momentum) * grad
                             + self.momentum * self.grad_old)
            if self.decay_exponent == 0:
                effective_lr = self.learning_rate
            else:
                effective_lr = (self.learning_rate
                                / (self.t + 1) ** self.decay_exponent)
            self.w += effective_lr * self.grad_old

        self.t += 1.

    def fit(self, X, Y, constraints=None):
        """Learn parameters using subgradient descent.

        Parameters
        ----------
        X : iterable
            Traing instances. Contains the structured input objects.
            No requirement on the particular form of entries of X is made.

        Y : iterable
            Training labels. Contains the strctured labels for inputs in X.
            Needs to have the same length as X.

        constraints : None
            Discarded. Only for API compatibility currently.
        """
        print("Training primal subgradient structural SVM")
        self.w = getattr(self, "w", np.zeros(self.problem.size_psi))
        objective_curve = []
        try:
            # catch ctrl+c to stop training
            for iteration in xrange(self.max_iter):
                if self.n_jobs == 1:
                    objective, positive_slacks = self._sequential_learning(X,
                                                                           Y)
                else:
                    objective, positive_slacks = self._parallel_learning(X, Y)

                # some statistics
                objective *= self.C
                objective += np.sum(self.w ** 2) / 2.

                if positive_slacks == 0:
                    print("No additional constraints")
                    if self.break_on_no_constraints:
                        break
                if self.verbose > 0:
                    print(self)
                    print("iteration %d" % iteration)
                    print("positive slacks: %d,"
                          "objective: %f" %
                          (positive_slacks, objective))
                objective_curve.append(objective)

                if self.verbose > 2:
                    print(self.w)

                self._compute_training_loss(X, Y, iteration)
                if self.logger is not None:
                    self.logger(self, iteration)

        except KeyboardInterrupt:
            pass
        self.objective_curve_ = objective_curve
        if objective_curve:
            print("final objective: %f" % objective_curve[-1])
        print("calls to inference: %d" % self.problem.inference_calls)
        return self

    def _parallel_learning(self, X, Y):
        n_samples = len(X)
        objective, positive_slacks = 0, 0
        verbose = max(0, self.verbose - 3)
        if self.batch_size is not None:
            raise ValueError("If n_jobs != 1, batch_size needs to"
                             "be None")
        # generate batches of size n_jobs
        # to speed up inference
        if self.n_jobs == -1:
            n_jobs = cpu_count()
        else:
            n_jobs = self.j_jobs

        n_batches = int(np.ceil(float(len(X)) / n_jobs))
        slices = gen_even_slices(n_samples, n_batches)
        for batch in slices:
            X_b = X[batch]
            Y_b = Y[batch]
            candidate_constraints = Parallel(
                n_jobs=self.n_jobs,
                verbose=verbose)(delayed(find_constraint)(
                    self.problem, x, y, self.w)
                    for x, y in zip(X_b, Y_b))
            dpsi = np.zeros(self.problem.size_psi)
            for x, y, constraint in zip(X_b, Y_b,
                                        candidate_constraints):
                y_hat, delta_psi, slack, loss = constraint
                if slack > 0:
                    objective += slack
                    dpsi += delta_psi
                    positive_slacks += 1
            self._solve_subgradient(dpsi, n_samples)
        return objective, positive_slacks

    def _sequential_learning(self, X, Y):
        n_samples = len(X)
        objective, positive_slacks = 0, 0
        if self.batch_size in [None, 1]:
            # online learning
            for x, y in zip(X, Y):
                y_hat, delta_psi, slack, loss = \
                    find_constraint(self.problem, x, y, self.w)
                objective += slack
                if slack > 0:
                    positive_slacks += 1
                self._solve_subgradient(delta_psi, n_samples)
        else:
            # mini batch learning
            n_batches = int(np.ceil(float(len(X)) / self.batch_size))
            slices = gen_even_slices(n_samples, n_batches)
            for batch in slices:
                X_b = X[batch]
                Y_b = Y[batch]
                Y_hat = self.problem.batch_loss_augmented_inference(
                    X_b, Y_b, self.w, relaxed=True)
                delta_psi = (self.problem.batch_psi(X_b, Y_b)
                             - self.problem.batch_psi(X_b, Y_hat))
                loss = np.sum(self.problem.batch_loss(Y_b, Y_hat))

                violation = loss - np.dot(self.w, delta_psi)
                objective += violation
                positive_slacks += self.batch_size
                self._solve_subgradient(delta_psi / len(X_b), n_samples)
        return objective, positive_slacks
